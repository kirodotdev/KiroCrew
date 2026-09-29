/** The conductor lane: the lineage seed poll, the lineage tree over every row (plus
 *  the member creators this page does not list), and which conductors are open. */
import { useMemo, useEffect, useState, useCallback, useRef } from 'react'
import { fetchSlots } from '../../store/dashboardSlice'
import type { AppDispatch } from '../../store'
import type { Slot } from './types'
import { buildLineage, ancestorsOf } from '../../lib/sessionLineage'
import { safeSetItem } from '../../utils/safeStorage'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import { readConductorExpanded, CONDUCTOR_EXPANDED_LS_KEY } from './persistence'
import type { ChatSlot } from '../../types'

/** Re-reads the slots while the lineage projection seeds; whether any row has a creator. */
export function useLineageSeed({ localSlots, dispatch, allRows }: {
  localSlots: Slot[]
  dispatch: AppDispatch
  allRows: Slot[]
}) {
  /**
   * Does any visible row actually have a creator that is also on screen?
   *
   * The conductor lane is only OFFERED when the answer is yes. A lane that renders
   * exactly the flat list, with a chevron nowhere, is a dead position in the toggle
   * cycle -- and the crew log can legitimately be off, in which case no row will ever
   * carry a parent. Read off `filteredSlots`, the same list the lane renders, so the
   * toggle never offers a lane the current filters have emptied of edges.
   */
  // While the gateway reports its lineage projection as still seeding, this frame's
  // `parent` values are provisional. The seed deliberately does not broadcast when it
  // lands (it would either write the slots coalescer's clock or add a frame, and both
  // are load-bearing there), so the recovery is a READ repeated from here. It matters
  // most on an IDLE gateway: with nothing running, no further frame is coming, and an
  // unnested cold start would otherwise persist until the user happened to act.
  //
  // Backed off 2s/4s/8s and abandoned after ~30s, because this is a cosmetic catch-up,
  // not a correctness loop: a seed that has not landed by then is not going to be fixed
  // by asking again, and a sidebar polling forever is worse than one that nests late.
  const lineagePending = useMemo(
    () => localSlots.some(s => s.lineage_pending === true),
    [localSlots],
  )
  useEffect(() => {
    if (!lineagePending) return
    let cancelled = false
    let attempt = 0
    const started = Date.now()
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = () => {
      if (cancelled || Date.now() - started > 30_000) return
      dispatch(fetchSlots())
      attempt += 1
      // 2s, 4s, 8s, then hold at 8s until the 30s budget runs out.
      timer = setTimeout(tick, Math.min(2000 * 2 ** attempt, 8000))
    }
    timer = setTimeout(tick, 2000)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [lineagePending, dispatch])

  // Read from the FULL row set, never from `filteredSlots`. The lane's own tree is
  // built from every row, so whether there is anything to nest is not a question a
  // filter gets to answer: computed from the narrowed list, turning on Unread could
  // hide every parent-carrying row, flip this false and take the whole lane away --
  // the nesting appeared and then vanished with no control touched that says so.
  const lineageAvailable = useMemo(
    () => allRows.some(s => s.parent?.key != null || s.parent?.slot),
    [allRows],
  )
  return { lineageAvailable }
}

/** The conductor lane population, lineage tree and open conductors. */
export function useConductorLane({ conductorLaneActive, allRows, allLiveSlots, isRowFolderHidden, laneOrder, flatSlots }: {
  conductorLaneActive: boolean
  allRows: Slot[]
  allLiveSlots: ChatSlot[]
  isRowFolderHidden: (s: Slot) => boolean
  laneOrder: (a: Slot, b: Slot) => number
  flatSlots: Slot[]
}) {
  /**
   * Live LOCAL sessions this page does not list, admitted to the conductor lane only
   * because a listed row cites one of them as its creator.
   *
   * `localSlots` is the CHAT PAGE's population: `ChatPage` filters the slot list by
   * surface before it reaches this component, and a crew member's own DM thread
   * (`surface: 'member'`) is not a chat-page session -- it lives on the Members page.
   * A member that dispatches workers through `session_create` is nonetheless the
   * creator every one of those workers cites, and the backend resolves that citation
   * to a live `parent.key` because the member's slot IS running. So the tree the
   * System page draws and the tree this lane drew came apart on exactly one kind of
   * creator: the lane never had the member's row, `nestsUnder` found no row for the
   * key, and every worker of a member-driven crew rendered at the top level wearing
   * the orphan glyph -- a glyph that says "opened by a closed session" about a
   * session that was open and working.
   *
   * The fix is to give the tree the row it is missing, and ONLY that row. Walked
   * transitively (a member's lead's workers still need the member), from the full
   * `dashboard.slots`, local rows only: a peer row's `parent.key` is in its own
   * gateway's key space and is resolved against the peer population already. These
   * rows are never MATCHES -- `conductorMatching` is built from `flatSlots`, which
   * cannot contain them -- so each renders as a dimmed anchor with its chevron, exactly
   * like a filtered-out conductor, and only while a descendant is on screen. Its click
   * goes to the Members page (see `renderSessionRow`), because the chat pane cannot
   * show it.
   */
  const creatorAnchors = useMemo((): Slot[] => {
    if (!conductorLaneActive) return []
    const listed = new Set<string>()
    for (const s of allRows) if (!isPeerRow(s)) listed.add(s.key)
    const live = new Map<string, Slot>()
    for (const s of allLiveSlots as unknown as Slot[]) if (!isPeerRow(s) && s.key) live.set(s.key, s)
    const out: Slot[] = []
    const added = new Set<string>()
    // Frontier: every listed local row, then each anchor as it is admitted, so a
    // chain of unlisted creators is followed to its end. Bounded by the live
    // population: a key is admitted once and a cycle re-visits nothing.
    const frontier: Slot[] = allRows.filter(s => !isPeerRow(s))
    while (frontier.length > 0) {
      const row = frontier.pop()!
      const cited = row.parent?.key
      if (cited == null || listed.has(cited) || added.has(cited)) continue
      const creator = live.get(cited)
      if (creator === undefined) continue
      added.add(cited)
      out.push(creator)
      frontier.push(creator)
    }
    return out
  }, [conductorLaneActive, allRows, allLiveSlots])

  /**
   * The conductor lane's whole population: what this page lists, plus the creators
   * it does not list but must draw for their workers to hang from.
   */
  const lanePopulation = useMemo(
    () => (creatorAnchors.length === 0 ? allRows : [...allRows, ...creatorAnchors]),
    [allRows, creatorAnchors],
  )

  /**
   * Which cited creators exist at all, as `origin -> set of slot keys` over the
   * UNFILTERED population.
   *
   * The conductor lane's citation glyph has two readings, and only this tells them
   * apart. A row placed under nothing because its creator is GONE is an orphan, and the
   * glyph says the creator is closed. A row placed under nothing because its creator is
   * merely concealed has a creator that is open and running, so the same glyph would
   * state something false about a live session. Read against the population before any
   * concealment, a present creator means the lane is simply not nesting -- which is what
   * `citesParent` says.
   */
  const citedCreatorExists = useMemo(() => {
    const byOrigin = new Map<string | undefined, Set<string>>()
    for (const s of lanePopulation) {
      let inOrigin = byOrigin.get(s.peer_id)
      if (inOrigin === undefined) {
        inOrigin = new Set<string>()
        byOrigin.set(s.peer_id, inOrigin)
      }
      inOrigin.add(s.key)
    }
    return byOrigin
  }, [lanePopulation])

  // ── conductor lane ───────────────────────────────────────────────────────
  //
  // Nests each session under the session that OPENED it. A different axis from
  // folders: a conductor and the workers it spawned are one unit of work wherever
  // their folders put them, and today they scatter through a recency-sorted list.

  /**
   * Every live session, in the lane's order -- NOT the filtered list.
   *
   * The tree has to be built over the whole population, because an edge is a fact about
   * two sessions and not about the current filter. Built from `filteredSlots`, a
   * conductor the filter did not admit was simply absent, so every worker it opened
   * resolved no parent and popped to the top level as an orphan: switching on Unread
   * scattered a conductor's workers across the lane, and the System page nested all of
   * them at the same moment. `lanePopulation` widens that once more, to the creators
   * this PAGE never lists (see `creatorAnchors`), for the same reason.
   *
   * Order is `laneOrder`, the comparator `filteredSlots` itself sorts by, so root and
   * sibling order still match the flat lane.
   *
   * The VIEW filters are what that whole population spans: status, tag and search decide
   * what the lane is ABOUT, so a row they exclude still belongs in the tree and renders
   * as a dimmed context anchor. The FOLDER filter is a stronger statement -- the person
   * asked not to see that folder -- so `isRowFolderHidden` applies HERE, to the
   * population itself. A concealed row is then absent from the tree, from `byKey` and
   * from every set derived downstream, so no render site has to ask a second time; its
   * children resolve no parent and fall back to the orphan-root treatment this lane
   * already gives a child whose parent it cannot show. The reveal row at the bottom of
   * the lane stays the way to look inside a hide.
   */
  const conductorRows = useMemo(() => {
    if (!conductorLaneActive) return []
    return [...lanePopulation].filter(s => !isRowFolderHidden(s)).sort(laneOrder)
  }, [conductorLaneActive, lanePopulation, laneOrder, isRowFolderHidden])

  /**
   * Row identities the active filter ADMITS, as the flat lane computed them.
   *
   * The filter still decides what the lane is about; it just no longer decides what the
   * tree is. A row in this set is a match and renders normally; a row outside it renders
   * only when something under it matched, and then as a dimmed anchor.
   */
  const conductorMatching = useMemo(
    () => new Set(flatSlots.map(sessionRowIdentity)),
    [flatSlots],
  )

  /**
   * The lineage tree over the rows this lane renders.
   *
   * Only computed while the lane is active: cheap, but still per-render work for a
   * view nobody is looking at.
   */
  const lineage = useMemo(() => {
    if (!conductorLaneActive) return null
    // The tree is keyed by `sessionRowIdentity`, not by raw slot key: this list mixes
    // local rows with rows federated from a peer, and the two namespaces collide on
    // deterministic keys. Keyed raw, one of a colliding pair replaces the other -- a
    // session disappears and its twin renders twice.
    //
    // A citation needs the same care from the other direction. `parent.key` is a bare
    // slot key in the key space of the CHILD's own gateway, so the creator is the row
    // carrying that key with the SAME origin. Resolving through this index rather than
    // composing the qualified form keeps that format the server's alone, and makes a
    // peer row unable to nest under a local row whose key merely matches.
    //
    // Nested by origin rather than keyed on one joined string: there is then no
    // separator, so no peer id or slot key containing it can be read as the wrong pair.
    const byOrigin = new Map<string | undefined, Map<string, string>>()
    for (const s of conductorRows) {
      let inOrigin = byOrigin.get(s.peer_id)
      if (inOrigin === undefined) {
        inOrigin = new Map<string, string>()
        byOrigin.set(s.peer_id, inOrigin)
      }
      inOrigin.set(s.key, sessionRowIdentity(s))
    }
    return buildLineage(conductorRows, {
      identityOf: sessionRowIdentity,
      parentIdentityOf: s => {
        const cited = s.parent?.key
        if (cited == null) return null
        return byOrigin.get(s.peer_id)?.get(cited) ?? null
      },
    })
  }, [conductorLaneActive, conductorRows])

  /**
   * Which conductor rows are open. COLLAPSED by default, and the open ones persist.
   *
   * Collapsed is the default because this lane exists to make a conductor and its
   * workers read as ONE unit of work: a crew of fourteen is one row with a count and
   * the subtree's badges on it, and the workers appear when the person asks for them.
   * The System page's Sessions tab is the place to see every row at once. The set
   * holds what the user has OPENED, so it survives a reload -- somebody who opened a
   * crew to watch it has not changed their mind -- while a conductor it has never held
   * renders shut.
   */
  const [conductorExpanded, setConductorExpanded] = useState<Set<string>>(readConductorExpanded)
  const persistConductorExpanded = useCallback((next: Set<string>) => {
    safeSetItem(CONDUCTOR_EXPANDED_LS_KEY, JSON.stringify(Array.from(next)))
  }, [])
  const toggleConductorExpanded = useCallback((key: string) => {
    setConductorExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      persistConductorExpanded(next)
      return next
    })
  }, [persistConductorExpanded])

  /**
   * Open every ancestor of *key* so a nested row becomes visible.
   *
   * The conductor-lane counterpart of `expandFolderAncestors`, and needed for the same
   * reason: revealing a row three levels down is pointless if the two rows above it
   * are collapsed. Persisted like any other expand -- a reveal is a real navigation,
   * not a peek.
   *
   * Reads the tree through a REF so this callback's identity never changes: it is a
   * dependency of the reveal effect, and a new identity on every slots broadcast would
   * re-run that effect continuously.
   */
  const lineageParentsRef = useRef<Map<string, string>>(new Map())
  lineageParentsRef.current = lineage?.parentOf ?? lineageParentsRef.current
  const expandConductorAncestors = useCallback((key: string) => {
    setConductorExpanded(prev => {
      const chain = ancestorsOf(key, lineageParentsRef.current)
      if (chain.length === 0 || chain.every(k => prev.has(k))) return prev
      const next = new Set(prev)
      for (const k of chain) next.add(k)
      persistConductorExpanded(next)
      return next
    })
  }, [persistConductorExpanded])

  /**
   * The creator each row cited on the PREVIOUS frame, so a row that MOVED can be told
   * from a row that is merely new.
   *
   * Holds `parent.slot`, the child's own citation, and not the placed parent: the
   * citation is a fact from that session's crew log, so it does not move when a search
   * or a folder filter changes which rows are in the payload. The placed parent does,
   * and diffing it would read a cleared search -- which restores every row's creator at
   * once -- as a whole sidebar's worth of moves.
   */
  const citedCreatorRef = useRef<Map<string, string | null>>(new Map())

  /**
   * A row whose creator CHANGED opens the row it moved under.
   *
   * Collapsed-by-default is right for a session the user opened, and wrong for one that
   * moves on its own: `session_adopt` re-parents a session that is already on screen, so
   * under a collapsed new parent the rows the person was watching unmount and leave a
   * child count behind. They did not collapse anything, so nothing tells them where the
   * sessions went. The primary flow of the feature would hide its own result.
   *
   * A CHANGED citation is what separates the two. A row absent from the last frame is a
   * creation -- `session_create`, which keeps the collapsed default and is untouched
   * here -- while a row that was already listed under one creator and now names another
   * was moved by someone other than the person looking at it. A citation that went to
   * null is a release: the row returns to the top level, where nothing needs opening.
   *
   * Expands the whole ancestor chain, not just the new parent: an adopter nested under a
   * collapsed conductor of its own would otherwise be as invisible as before. That is
   * `expandConductorAncestors`, the same walk a reveal uses, applied to the row that
   * moved.
   *
   * Bookkeeping runs on EVERY frame, including while the lane is not rendering, and only
   * the expansion is gated on it. Dropping the map when the lane is off looked harmless
   * and was not: a saved conductor view with no lineage yet suppresses the lane, so the
   * map was cleared every frame and the FIRST adoption's frame found no previous citation
   * for the row -- read as a creation, which keeps the collapsed default and hides the
   * very row that moved. The baseline has to predate the move, so it cannot be seeded by
   * the frame that carries it.
   *
   * The tracked set is EVERY row, not the filtered one. A row the filter excludes can
   * still be on screen -- the lane keeps it as a dimmed anchor when something under it
   * matched -- so reading only the filtered set would miss exactly those rows' moves, and
   * the promise above is what would break: an anchor adopted under a collapsed conductor
   * would take its subtree off screen with nothing opening the destination.
   *
   * The next frame's map also CARRIES the previous one forward rather than replacing it,
   * because a row absent from one frame has not necessarily gone anywhere. Rebuilding
   * from a single frame would evict its baseline, and a later reappearance would read as
   * a creation rather than a move -- the same row-hiding failure by another route. A row
   * that is genuinely gone is dropped by the lane unmounting, not by one thin frame.
   */
  useEffect(() => {
    const previous = citedCreatorRef.current
    const current = new Map<string, string | null>(previous)
    const moved: string[] = []
    for (const slot of lanePopulation) {
      // A provisional row is not a baseline. The cold-start frame ships every row with
      // `parent: null` and `lineage_pending` while the gateway's projection seeds; the
      // frame that settles it then carries the real citations. Recording the nulls would
      // read every null -> key transition as a MOVE and persist every crew open on every
      // reload, inverting the collapsed default. Skipped, the settling frame is the
      // row's first sighting -- a creation -- and a row seen settled before keeps the
      // baseline it already had.
      if (slot.lineage_pending === true) continue
      const identity = sessionRowIdentity(slot)
      const cited = slot.parent?.slot ?? null
      current.set(identity, cited)
      if (!previous.has(identity)) continue
      if (previous.get(identity) === cited || cited == null) continue
      moved.push(identity)
    }
    citedCreatorRef.current = current
    // Only the expansion is conditional: expanding a lane nobody is looking at changes
    // nothing a user can see, while recording the citation is what makes the NEXT frame
    // able to tell a move from a creation.
    if (!conductorLaneActive || lineage == null) return
    for (const identity of moved) expandConductorAncestors(identity)
  }, [conductorLaneActive, lineage, lanePopulation, expandConductorAncestors])
  return {
    citedCreatorExists, conductorRows, conductorMatching, lineage, conductorExpanded, toggleConductorExpanded,
    expandConductorAncestors,
  }
}
