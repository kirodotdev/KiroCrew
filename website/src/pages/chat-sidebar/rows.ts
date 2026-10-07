/** The sidebar's row model: which rows pass the filters and in what order, which lane
 *  and folder each one lands in, and what each rendered row shows.
 *
 *  The list mixes two origins. A LOCAL row is this machine's slot; a PEER row is a live
 *  session a connected crew owns (`peer_id`), and its raw `key` can be byte-identical to
 *  a local one because the two gateways do not share a key namespace. Every piece of
 *  local sidebar state here (folder, pin, pinned rank, unread, running, recent, search
 *  rank, rename, reveal flash, digit badge, popped-out, sub-agent counts, the active
 *  slot) is indexed by LOCAL key. This module is where that state is read for a row, and
 *  it is read only after the one origin check in `localKeyOf`, so a peer row never
 *  carries another session's local state into a filter, a lane, a folder or a
 *  `SessionRowView`.
 *
 *  Pure: no React, no storage, no clock of its own (`clock` is injected). The facade
 *  calls `buildSidebarRows` on every render with the previous result; each memoized
 *  output (the lists, the lane populations, the order, the predicates, the reveal
 *  registry, the chip counts) keeps its identity while the inputs it reads are
 *  unchanged (the same `Object.is` test a `useMemo` dependency list applies), so an
 *  effect keyed on `filteredSlots` or a memo keyed on `laneOrder` re-runs only when that
 *  output can have changed. The result object, its `lanes` container, `staleSplit` and
 *  `views` are fresh per build: read their members, never key on them. */
import type { ChatTag } from '../../types'
import type { PaletteBoost } from '../../utils/sessionColors'
import { isChatPageSurface } from '../../utils/channelOrigin'
import { compareBySort, comparePinnedThenSort, lastActivityEpoch, slotActivityTs, type SortKey } from '../chat/sessionOrder'
import { crewOf } from '../../hooks/useInstanceSessions'
import { isWithinRecentWindow } from '../recentWindow'
import { splitStaleSlots, type StaleSplit } from '../staleCollapse'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import type { AgentInfo, FilterDimension, RevealBlockingFilter, SessionFilterKey, SidebarSourceLink, Slot } from './types'

/** What the chip prints. The serializer decides it; this only covers its absence,
 * which means a bundle newer than the gateway it is talking to.
 *
 * Deliberately DUMB -- `#N` for anything, with no provider branch. Reaching for the
 * provider's real convention here would be a second implementation of the
 * serializer's rule, and a fallback that is a near-copy of the real rule is the kind
 * that drifts silently. `#N` is recognisably the object and recognisably generic. The
 * search box matches this same label, so a chip and the query that finds it cannot
 * disagree. */
export function chipLabel(link: SidebarSourceLink): string {
  return link.label ?? `#${link.number}`
}

/** Every piece of local sidebar state a filter, a lane or the order reads, each
 *  indexed by LOCAL slot key. Never consulted for a peer row. */
export interface LocalRowIndex {
  /** Local slot key -> the folder it is filed in (valid folders only). */
  folderOf: Readonly<Record<string, string>>
  pinned: ReadonlySet<string>
  /** Position inside the pinned section (the manual pinned order). */
  pinnedRank: ReadonlyMap<string, number>
  unread: ReadonlySet<string>
  /** The widened running signal: own turn, a live workflow or an active loop. */
  running: ReadonlySet<string>
  /** Inside the Recent window (a running row is recent by definition). */
  recent: ReadonlySet<string>
  /** The backend's relevance order while a content search is active, else null. */
  searchRanks: ReadonlyMap<string, number> | null
}

export interface SidebarRowFilters {
  /** `resolved` narrows (only ids the loaded vocabulary knows); `raw` is the stored
   *  selection a reveal clears, so a reveal arriving before the vocabulary loads still
   *  clears it instead of leaving the row to be re-hidden mid-flight. */
  tags: { resolved: ReadonlySet<string>; raw: ReadonlySet<string> }
  /** The filter menu's tag HIDES (#13801): a row carrying any of these tags is
   *  dropped. `resolved` / `raw` split exactly as for `tags`. */
  tagHides: { resolved: ReadonlySet<string>; raw: ReadonlySet<string> }
  /** The search box. `folderMatches` is the folders (with their subtrees) whose NAME
   *  the query matched, or null when none did. */
  search: { text: string; folderMatches: ReadonlySet<string> | null }
  /** The status chips. A paused set keeps its chips and narrows nothing. */
  status: { active: ReadonlySet<SessionFilterKey>; paused: boolean; recentWindowMs: number }
  /** The filter menu's folder hides. `active` is false while the search box holds
   *  text, so a hidden folder never becomes a search dead end. */
  folders: { hiddenSubtree: ReadonlySet<string>; active: boolean }
}

/** How a reveal drops each dimension. Carried, never called, by this module. */
export interface SidebarRowClears {
  tags: () => void
  tagHides: () => void
  search: () => void
  status: () => void
  /** Un-hide this folder and its ancestor chain; a no-op for `undefined`. */
  folder: (folderId: string | undefined) => void
}

export interface SidebarRowInputs {
  /** The rendered row set: local tabs plus live peer rows, deduplicated by identity. */
  rows: Slot[]
  local: LocalRowIndex
  filters: SidebarRowFilters
  clears: SidebarRowClears
  /** The per-machine crew groups the list shows, in order (none on the board, which
   *  groups nothing). A filtered row whose crew (`crewOf`: the peer that owns it, or
   *  the crew a local slot's turns run on) has a group here leaves the `Local` lanes
   *  and renders under that group instead. */
  crewGroups: readonly { id: string }[]
  sortKey: SortKey
  /** Read lazily, at the moment a peer row's recency or a stale split is asked for. */
  clock: () => number
  /** While a drag is in flight the lanes keep the order they had when it started:
   *  `filteredSlots` stays the previous result's list until the drag ends. */
  frozen: boolean
}

/** The conductor lane's additions to a row, and nothing more. Every field is a fact
 *  the LANE knows and the row cannot: how deep it sits, how many sessions it opened,
 *  whether those are hidden right now, and what its subtree asks for while they are.
 *  The row renders them and derives none of them, so the flat lane's row and this one
 *  stay one component with the same data. They are handed INTO the row rather than
 *  wrapped around it: a wrapper that put the chevron and the counts beside the card
 *  narrowed the card itself. Compared by value, so a lane render that rebuilds this
 *  object leaves an unchanged row alone. */
export interface ConductorRowView {
  /** 0 for a root. Indents the row up to the facade's indent cap. */
  depth: number
  /** Direct children. 0 renders no chevron and no count. */
  childCount: number
  expanded: boolean
  /** The collapsed subtree's asks, or null while it is open -- an open conductor's
   *  children show their own, and both at once would count a session twice. */
  aggregate: { needsYou: number; running: number } | null
  /** The creator this row cites but could not nest under, because it has closed. */
  orphanOf: string | null
  /** The creator this row cites while the lane is NOT nesting it -- search flattens
   *  every match to one level, so a child would otherwise be indistinguishable from a
   *  session nobody opened. Distinct from `orphanOf`: that creator is gone, this one
   *  is present and simply not above this row right now. */
  citesParent: string | null
  /** On screen only to hold its workers together: the active filter does not admit
   *  it, but something in its subtree needs it as the row the nesting hangs from. */
  anchorOnly: boolean
}

/** Where one rendered copy of a row sits. A row can render in several places at once
 *  (one board column per matching tag), so these are per placement, not per row. */
export interface RowPlacement {
  /** Namespaces the layoutId and the inline rename target per render location. */
  scope: string
  navScope: string
  holdContainer: string
  showDivider: boolean
  /** Paint-order stamp, clamped by the facade. The row never reads it: it exists so a
   *  row whose position moved compares unequal and re-renders for the layout spring. */
  orderStamp: number
  /** Layout projection on for this copy (the facade's displacement-window gate). */
  rowAnimEnabled: boolean
  conductor?: ConductorRowView
}

/** The shell values every row shows the same way. The shell builds it once per change
 *  of any member, and a view holds it by reference, so a change here re-renders every
 *  row and nothing else does. */
export interface RowShell {
  connected: boolean
  mode: string | undefined
  /** A phone-width viewport or any touch screen: the single ⋯ menu instead of the
   *  hover-revealed action cluster. */
  isMobile: boolean
  colorMode: string
  defaultAgent: string
  installedAgents: AgentInfo[]
  tagById: Record<string, ChatTag>
  paletteColors: string[]
  boost: PaletteBoost
  boostFor: (hex: string) => PaletteBoost
  recentTintCount: number
  /** A dnd-kit drag is in flight, the raw LOCAL key of a dragged session (only local
   *  rows can be dragged), and that session's pinned rank. */
  dragInFlight: boolean
  activeDraggedKey: string | null
  activeDraggedPinnedIndex: number
}

/** The values one render shows rows with. */
export interface RowScene {
  /** The LOCAL slot the chat pane shows. */
  activeSlot: string | null
  /** The crew window open over the pane, naming a peer's own slot by (crew, key), or
   *  null. While one is open it is the active row, and no local row is. */
  crewWindow: { instanceId: string; key: string } | null
  shell: RowShell
  /** Local state that only some rows carry, every member keyed by LOCAL slot key. */
  local: {
    recentRank: ReadonlyMap<string, number>
    subagentCounts: Readonly<Record<string, number>>
    subagentApprovalCounts: Readonly<Record<string, number>>
    poppedOut: ReadonlySet<string>
    /** The chat-jump digits while the modifier is held, else null. */
    digits: ReadonlyMap<string, string> | null
    /** The inline rename in progress: which slot, in which placement, and its draft. */
    renaming: { key: string | null; scope: string | null; value: string }
    revealFlash: { kind: string; key: string; fading: boolean } | null
    /** The board card whose native HTML5 drag is in flight. */
    nativeDragKey: string | null
  }
  /** The dnd-kit drag in flight, if any. */
  drag: { type: string; id: string } | null
}

/** Everything one session row renders from, already resolved for its origin. A peer
 *  view's local fields hold what a row with no local state shows. */
export interface SessionRowView {
  slot: Slot
  /** Origin-qualified: unique across local and peer rows. */
  identity: string
  /** The key the row's own slot-scoped store subscriptions read; '' for a peer row,
   *  whose raw key names a DIFFERENT local session's live state. */
  localKey: string
  /** The owning crew, set only on a peer row: every local-only affordance is gated on
   *  its absence. */
  peerId: string | undefined
  peerName: string | undefined
  scope: string
  navScope: string
  holdContainer: string
  showDivider: boolean
  orderStamp: number
  rowAnimEnabled: boolean
  conductor: ConductorRowView | null
  isActive: boolean
  isOut: boolean
  isPinned: boolean
  isUnread: boolean
  /** A peer row reports its own `running`; a local row the widened running set. */
  isRunning: boolean
  recent: number | undefined
  subagentCount: number
  subagentApprovalCount: number
  digitBadge: string | undefined
  isRenaming: boolean
  /** The inline edit is pinned to THIS placement. */
  renamingHere: boolean
  /** The live draft on the one placement being renamed, '' everywhere else, so a
   *  keystroke re-renders one row instead of all of them. */
  renameValue: string
  revealFlash: 'flash' | 'fade' | null
  pinnedOrderIndex: number
  pinnedReorderEnabled: boolean
  /** A LOCAL row whose surface this page does not render (a crew member's own DM
   *  thread, admitted to the conductor lane as a creator anchor) opens here instead
   *  of switching to it; null for every other row. */
  opensElsewhere: string | null
  shell: RowShell
}

/** What the windowed stub of a row needs: its title, and whether the row must never
 *  stub because it holds state a remount would drop. Kept out of `SessionRowView`, so
 *  a change that only the window answers (a native drag starting) does not re-render
 *  the row. */
export interface RowWindow {
  title: string
  keepMounted: boolean
}

export interface SidebarRowViews {
  /** The active-row highlight: the peer row the open crew window names by its (crew,
   *  key) pair, else the LOCAL row whose key is the active slot. */
  isActive: (slot: Slot | null | undefined) => boolean
  view: (slot: Slot, placement: RowPlacement) => SessionRowView
  windowOf: (slot: Slot) => RowWindow
}

export interface SidebarRows {
  /** Every row passing every filter dimension, in lane order. */
  filteredSlots: Slot[]
  /** Is any dimension narrowing the list right now? */
  listNarrowed: boolean
  /** One entry per dimension, in declaration order: does it hide THIS row, and how to
   *  drop it. The reveal effect walks this instead of naming the dimensions. */
  revealBlockingFilters: RevealBlockingFilter[]
  /** Status-chip badges, counted over every row (the collection the filter renders). */
  statusCounts: Record<SessionFilterKey, number>
  /** The one order every lane uses: the search relevance while a content search is
   *  active, otherwise the local pinned section first, then the sort key. */
  laneOrder: (a: Slot, b: Slot) => number
  /** Does the folder filter conceal this row?
   *
   *  ONE definition because every lane that renders sessions has to ask it, and each
   *  asks it where it builds its row POPULATION rather than at its render site: the
   *  tree lane drops an unchecked folder's whole block, the flat lane and the board
   *  strip the rows, and the conductor lane keeps them out of its lineage. Asked at a
   *  render site instead, a lane would have to remember to ask again for every set it
   *  derives -- its matches, its context anchors, its collapsed aggregates -- and the
   *  one it forgot would put a row on screen the person asked not to see.
   *
   *  Distinct from the folder's OWN hide-when-empty attribute. This one is the
   *  person's choice in the filter menu, and it is off entirely while the search box
   *  has text, so a hidden folder never becomes a search dead-end. */
  isRowFolderHidden: (slot: Slot) => boolean
  lanes: {
    /** The rows the `Local` lanes draw: every filtered row no shown crew group holds
     *  (all of them while no group shows). Every lane below, and the folder index,
     *  starts from this list. */
    main: Slot[]
    /** Each shown crew group's rows, in lane order, the folder hide applied (crew rows
     *  obey the folder filter like every lane's own rows). A key for every shown group,
     *  empty or not. */
    crew: ReadonlyMap<string, Slot[]>
    /** How many rows the crew groups show in all. */
    crewRowCount: number
    /** The flat lane: the `Local` rows minus every row the folder filter conceals. */
    flat: Slot[]
    /** The tree lane's root bucket: every `Local` row filed in no local folder. */
    ungrouped: Slot[]
    /** The board's pool: the `Local` LOCAL-origin rows the folder filter does not
     *  conceal. A board column writes tags and state, which no peer row can take, and a
     *  column has no reveal row, so the hide is absolute there. */
    board: Slot[]
    /** `Local` peer rows the board leaves out, for its notice. */
    boardPeerCount: number
  }
  folderTree: {
    /** The `Local` rows filed directly in this folder, in lane order. */
    rowsIn: (folderId: string) => Slot[]
  }
  /** The local folder this row is filed in; undefined for a peer row. */
  folderOf: (slot: Slot) => string | undefined
  /** Locally pinned; false for a peer row. */
  isPinned: (slot: Slot) => boolean
  /** A peer row's own `running`, else the widened local running set. */
  isRunning: (slot: Slot) => boolean
  /** The dormant-session split of one container's rows. Inert while the list is
   *  narrowed (a filter must reach every match) and under any sort but newest-first,
   *  which is the only order whose stale rows form a truthful contiguous tail. */
  staleSplit: (list: Slot[], stale: { collapseMs: number; isExempt: (slot: Slot) => boolean }) => StaleSplit<Slot>
  views: (scene: RowScene) => SidebarRowViews
}

/** THE origin check. A peer row has no local key, whatever its raw key spells. */
function localKeyOf(slot: Pick<Slot, 'key' | 'peer_id'>): string | null {
  return isPeerRow(slot) ? null : slot.key
}

interface Memo<T> { deps: readonly unknown[]; value: T }

/** `previous`'s value while every dependency is identical, the `useMemo` rule. */
function reuse<T>(previous: Memo<T> | undefined, deps: readonly unknown[], compute: () => T): Memo<T> {
  if (previous && previous.deps.length === deps.length && previous.deps.every((d, i) => Object.is(d, deps[i]))) return previous
  return { deps, value: compute() }
}

interface BuildMemos {
  folderOf: Memo<SidebarRows['folderOf']>
  isPinned: Memo<SidebarRows['isPinned']>
  isRunning: Memo<SidebarRows['isRunning']>
  laneOrder: Memo<SidebarRows['laneOrder']>
  statusMatches: Memo<Record<SessionFilterKey, (slot: Slot) => boolean>>
  dimensions: Memo<FilterDimension[]>
  statusCounts: Memo<Record<SessionFilterKey, number>>
  filteredSlots: Memo<Slot[]>
  revealBlockingFilters: Memo<RevealBlockingFilter[]>
  isRowFolderHidden: Memo<SidebarRows['isRowFolderHidden']>
  crewRows: Memo<Map<string, Slot[]>>
  main: Memo<Slot[]>
  crew: Memo<ReadonlyMap<string, Slot[]>>
  crewRowCount: Memo<number>
  flat: Memo<Slot[]>
  ungrouped: Memo<Slot[]>
  board: Memo<Slot[]>
  boardPeerCount: Memo<number>
  folderTree: Memo<SidebarRows['folderTree']>
}

const memosOf = new WeakMap<SidebarRows, BuildMemos>()
const NO_ROWS: Slot[] = []

// Every closure the model hands out is made by a module-level factory below, from only
// the values it is declared to read. A closure created inside `buildSidebarRows` would
// share that call's whole scope -- its rows, and the previous result it was given -- so
// an output reused across builds would hold every earlier build alive behind it.

function makeFolderOf(slotFolders: Readonly<Record<string, string>>): SidebarRows['folderOf'] {
  return slot => {
    const key = localKeyOf(slot)
    return key === null ? undefined : slotFolders[key]
  }
}

function makeIsPinned(pinned: ReadonlySet<string>): SidebarRows['isPinned'] {
  return slot => {
    const key = localKeyOf(slot)
    return key !== null && pinned.has(key)
  }
}

function makeIsRunning(running: ReadonlySet<string>): SidebarRows['isRunning'] {
  return slot => {
    const key = localKeyOf(slot)
    return key === null ? slot.running === true : running.has(key)
  }
}

function makeLaneOrder(
  searchRanks: ReadonlyMap<string, number> | null,
  sortKey: SortKey,
  pinned: ReadonlySet<string>,
  pinnedRank: ReadonlyMap<string, number>,
  isPinned: SidebarRows['isPinned'],
): SidebarRows['laneOrder'] {
  // Search relevance, not the sort: pinning is a reachability promise for browsing,
  // not a ranking hint inside explicit search results. A peer row is never ranked,
  // because the ranks are keyed by LOCAL key.
  const rank = (slot: Slot) => {
    const key = localKeyOf(slot)
    return (key !== null && searchRanks ? searchRanks.get(key) : undefined) ?? Infinity
  }
  return (a, b) => {
    if (searchRanks) return rank(a) - rank(b)
    // The pinned SECTION is decided here, per origin; the pinned ORDER inside it
    // still has exactly one implementation, delegated to below.
    const aPinned = isPinned(a)
    const bPinned = isPinned(b)
    if (aPinned !== bPinned) return aPinned ? -1 : 1
    if (aPinned && bPinned) return comparePinnedThenSort(a, b, sortKey, pinned, pinnedRank)
    return compareBySort(a, b, sortKey)
  }
}

type StatusMatches = Record<SessionFilterKey, (slot: Slot) => boolean>

/** Exhaustive over `SessionFilterKey`: a new chip key is a type error here instead of
 *  a predicate that silently matches nothing. A peer row answers from its own fields. */
function makeStatusMatches(
  unread: ReadonlySet<string>,
  isRunning: SidebarRows['isRunning'],
  recent: ReadonlySet<string>,
  recentWindowMs: number,
  clock: () => number,
): StatusMatches {
  return {
    unread: slot => { const key = localKeyOf(slot); return key !== null && unread.has(key) },
    running: isRunning,
    pinned: slot => localKeyOf(slot) !== null && !!slot.pinned,
    recent: slot => {
      const key = localKeyOf(slot)
      return key === null
        ? isWithinRecentWindow(slotActivityTs(slot), clock(), recentWindowMs)
        : recent.has(key)
    },
  }
}

/**
 * THE single declaration of every filter dimension. `filteredSlots`, `listNarrowed`
 * and `revealBlockingFilters` all derive from this list, so adding a dimension is one
 * entry here -- the required fields force a decision per consumer, and those three
 * consumers cannot drift because none of them enumerates dimensions itself.
 *
 * The consumers legitimately answer different questions:
 * - the folder dimension filters no rows (`filtersRow: null` -- it drops whole folder
 *   blocks and lanes through `isRowFolderHidden` and the facade's folder render) and
 *   never narrows (`narrows: null`);
 * - tags narrow by the RESOLVED ids but hide by the raw stored ones (see
 *   `SidebarRowFilters.tags`).
 */
function makeDimensions(
  filters: SidebarRowFilters,
  matches: StatusMatches,
  searchRanks: ReadonlyMap<string, number> | null,
  folderOf: SidebarRows['folderOf'],
  clears: SidebarRowClears,
): FilterDimension[] {
  const { tags, tagHides, search, status, folders: folderFilter } = filters
  // A paused set keeps its chips but narrows nothing, so the whole status dimension
  // goes inert while the pause is on.
  const activeStatus = status.paused ? [] : (Object.keys(matches) as SessionFilterKey[]).filter(key => status.active.has(key))
  // A pinned session is one the person chose to keep in sight, and the pinned band is
  // the stable top of the list (`laneOrder`). The two property filters (tags, status
  // chips) therefore exempt it: the band stays, the narrowed rows sit under it, and the
  // chip counts still count matches (`countStatus` reads the predicates, not the
  // filtered list). The search is NOT exempt -- a query names one session, and a
  // non-matching pinned row in its results is noise -- and neither is a folder hide,
  // which means "hide all of this". Same predicate as the Pinned chip, so a peer row
  // (never pinned) is never exempt. `narrows` is untouched: the list IS narrowed; the
  // pinned rows are exempt from it.
  const pinnedBypass = matches.pinned
  return [
    {
      // Tags. Unlike the folder filter this does NOT go inert while searching: it
      // is a session property, so it behaves like the Unread/Pinned status chips.
      filtersRow: slot => tags.resolved.size === 0 || pinnedBypass(slot) || (slot.tags ?? []).some(id => tags.resolved.has(id)),
      narrows: () => tags.resolved.size > 0,
      // The pinned exemption is restated here because this predicate does not go
      // through `excluded`: a reveal of a pinned row must not clear a tag filter that
      // was never hiding it.
      hides: slot => tags.raw.size > 0 && !pinnedBypass(slot) && !(slot.tags ?? []).some(id => tags.raw.has(id)),
      clear: () => clears.tags(),
    },
    {
      // Text search: title and source links, never key/agent once the backend has
      // ranked (rows it excluded) -- a badge id is a card-visible PROPERTY.
      filtersRow: slot => {
        if (!search.text) return true
        const q = search.text.toLowerCase()
        // The row's own CONTAINER matched by name: the query named the folder, so
        // everything filed in it is what was asked for. A peer row is never in a
        // local folder.
        const container = folderOf(slot)
        if (search.folderMatches && container && search.folderMatches.has(container)) return true
        const titleMatch = (slot.title || '').toLowerCase().includes(q)
        // Both id spellings match by PREFIX, so progressive typing works while an
        // interior run of the digits -- an accident, not an id -- does not.
        const sourceMatch = (slot.source_links ?? []).some(link =>
          String(link.number).startsWith(q)
          || chipLabel(link).toLowerCase().startsWith(q))
        // The ranks are LOCAL keys: a peer row matches on its own visible fields.
        if (searchRanks) {
          const key = localKeyOf(slot)
          return (key !== null && searchRanks.has(key)) || titleMatch || sourceMatch
        }
        return sourceMatch
          || ((slot.title || '') + slot.key + (slot.agent || '')).toLowerCase().includes(q)
      },
      narrows: () => Boolean(search.text),
      hides: (slot, excluded) => Boolean(search.text) && excluded(slot),
      clear: () => clears.search(),
    },
    {
      // Status chips. Active chips OR together: a row passes when any active chip
      // matches it. A pinned row passes whatever the chips say (see `pinnedBypass`).
      // `hides` restates the exemption too: `excluded` is list membership, not this
      // dimension's verdict, so a pinned row the SEARCH dropped would otherwise read as
      // hidden by the chips and a reveal would clear them.
      filtersRow: slot => activeStatus.length === 0 || pinnedBypass(slot) || activeStatus.some(key => matches[key](slot)),
      narrows: () => activeStatus.length > 0,
      hides: (slot, excluded) => activeStatus.length > 0 && !pinnedBypass(slot) && excluded(slot),
      clear: () => clears.status(),
    },
    {
      // Folder filter: no row filtering and no narrowing (see above). Its clear
      // un-hides the row's own ancestor chain rather than clearing globally.
      filtersRow: null,
      narrows: null,
      hides: slot => {
        const folderId = folderOf(slot)
        return !!folderId && folderFilter.hiddenSubtree.has(folderId)
      },
      clear: slot => clears.folder(folderOf(slot)),
    },
    {
      // Tag hides (#13801). Its own dimension, not part of the tag entry above,
      // because it composes the other way: selected tags OR together, while a hide
      // drops its matches from whatever the rest of the list kept, so "Blocked +
      // hide Done" is Blocked AND not Done. Being separate also means a reveal clears
      // only the hide, never the tags the person selected beside it. Unlike the
      // include filter a hide applies to PINNED rows too: like a folder hide it means
      // "hide all of these", and pinning a Done session does not un-say that.
      // Appended LAST so the reveal registry's existing positions do not move.
      filtersRow: slot => tagHides.resolved.size === 0 || !(slot.tags ?? []).some(id => tagHides.resolved.has(id)),
      narrows: () => tagHides.resolved.size > 0,
      // Answers for one row on its own: it hides exactly the rows carrying a hidden
      // tag, so a row the search dropped never clears a hide that never matched it.
      hides: slot => tagHides.raw.size > 0 && (slot.tags ?? []).some(id => tagHides.raw.has(id)),
      clear: () => clears.tagHides(),
    },
  ]
}

/** Over every row -- the collection the filter RENDERS -- not over the local ones. The
 *  two diverge for `running` and `recent`, whose predicates match peer rows: counting
 *  locals made those two badges under-report by exactly the remote rows the filter goes
 *  on to show. `unread` and `pinned` are local-only predicates, so the wider collection
 *  cannot add a match to them. */
function countStatus(rows: Slot[], matches: StatusMatches): Record<SessionFilterKey, number> {
  const counts = {} as Record<SessionFilterKey, number>
  for (const key of Object.keys(matches) as SessionFilterKey[]) counts[key] = rows.filter(matches[key]).length
  return counts
}

/** Peer rows are narrowed and ordered by exactly the same rules as local ones; only
 *  the local state each rule reads is origin-checked. */
function filterRows(rows: Slot[], dimensions: FilterDimension[], laneOrder: SidebarRows['laneOrder']): Slot[] {
  return rows
    .filter(slot => dimensions.every(d => d.filtersRow === null || d.filtersRow(slot)))
    .sort(laneOrder)
}

/** Search and status defer to list membership: both rank against backend state
 *  (relevance, unread) that a single row cannot answer for alone. */
function makeRevealRegistry(dimensions: FilterDimension[], filteredSlots: Slot[]): RevealBlockingFilter[] {
  const shown = new Set(filteredSlots.map(sessionRowIdentity))
  const excluded = (slot: Slot) => !shown.has(sessionRowIdentity(slot))
  return dimensions.map(d => ({
    hides: (slot: Slot) => d.hides(slot, excluded),
    clear: d.clear,
  }))
}

function makeIsRowFolderHidden(
  active: boolean,
  hiddenSubtree: ReadonlySet<string>,
  folderOf: SidebarRows['folderOf'],
): SidebarRows['isRowFolderHidden'] {
  return slot => {
    if (!active) return false
    const folderId = folderOf(slot)
    return !!folderId && hiddenSubtree.has(folderId)
  }
}

/** Each shown crew group's filtered rows, in lane order: one key per group, so an
 *  empty group still has an entry. A row whose crew has no group stays out. */
function groupByCrew(groups: readonly { id: string }[], filteredSlots: Slot[]): Map<string, Slot[]> {
  const byCrew = new Map<string, Slot[]>(groups.map(g => [g.id, []]))
  for (const slot of filteredSlots) {
    const id = crewOf(slot)
    if (id) byCrew.get(id)?.push(slot)
  }
  return byCrew
}

function hideInCrews(byCrew: Map<string, Slot[]>, hidden: SidebarRows['isRowFolderHidden']): ReadonlyMap<string, Slot[]> {
  const out = new Map<string, Slot[]>()
  for (const [id, list] of byCrew) out.set(id, list.filter(slot => !hidden(slot)))
  return out
}

function makeFolderTree(filteredSlots: Slot[], folderOf: SidebarRows['folderOf']): SidebarRows['folderTree'] {
  const index = new Map<string, Slot[]>()
  for (const slot of filteredSlots) {
    const folderId = folderOf(slot)
    if (!folderId) continue
    const list = index.get(folderId)
    if (list) list.push(slot); else index.set(folderId, [slot])
  }
  return { rowsIn: folderId => index.get(folderId) ?? NO_ROWS }
}

function makeStaleSplit(listNarrowed: boolean, sortKey: SortKey, clock: () => number): SidebarRows['staleSplit'] {
  return (list, stale) => {
    const active = !listNarrowed && sortKey === 'date-desc'
    return splitStaleSlots(list, active ? stale.collapseMs : 0, clock(), slot => lastActivityEpoch(slot) * 1000, stale.isExempt)
  }
}

interface ViewModel {
  pinned: ReadonlySet<string>
  pinnedRank: ReadonlyMap<string, number>
  unread: ReadonlySet<string>
  isRunning: (slot: Slot) => boolean
  searchRanks: ReadonlyMap<string, number> | null
}

/** The active row. A peer row is active only for the crew window open on its own
 *  (crew, key) PAIR -- its raw key alone can equal another crew's, or a local slot's.
 *  A local row is active only while no crew window is open, because the window then
 *  covers the pane its slot would show. */
function isActiveIn(scene: RowScene, slot: Slot): boolean {
  const key = localKeyOf(slot)
  const win = scene.crewWindow
  if (key === null) return !!win && win.instanceId === slot.peer_id && win.key === slot.key
  return !win && scene.activeSlot === key
}

function makeViews(model: ViewModel): SidebarRows['views'] {
  return scene => {
    const isActive = (slot: Slot | null | undefined) => !!slot && isActiveIn(scene, slot)
    return {
      isActive,
      view: (slot, placement) => rowView(slot, placement, scene, model),
      windowOf: slot => {
        const key = localKeyOf(slot)
        const { renaming, revealFlash, nativeDragKey } = scene.local
        return {
          title: slot.title && slot.title !== slot.key ? slot.title : slot.key,
          // The active, renaming, dragged and revealed rows never stub: each holds state
          // a remount would drop. The dnd-kit clause compares the raw key, as the drag id
          // is one; no peer row can be dragged.
          keepMounted: isActive(slot)
            || (key !== null && revealFlash?.kind === 'session' && revealFlash.key === key)
            || (key !== null && renaming.key === key)
            || (scene.drag?.type === 'session' && scene.drag.id === slot.key)
            || (key !== null && nativeDragKey === key),
        }
      },
    }
  }
}

/** Build the row model. Pass the previous result to keep the identity of every memoized
 *  output whose inputs did not change (and, while `frozen`, its filtered list). */
export function buildSidebarRows(inputs: SidebarRowInputs, previous?: SidebarRows | null): SidebarRows {
  const prev = previous ? memosOf.get(previous) : undefined
  const { rows, local, filters, clears, crewGroups, sortKey, clock, frozen } = inputs
  const { folderOf: slotFolders, pinned, pinnedRank, unread, running, recent, searchRanks } = local
  const { tags, search, status, folders: folderFilter } = filters
  // Read here, outside every compute: the drag freeze is the one place a build reads
  // the previous one's output.
  const frozenList = frozen ? (prev?.filteredSlots.value ?? NO_ROWS) : null

  const folderOfMemo = reuse(prev?.folderOf, [slotFolders], () => makeFolderOf(slotFolders))
  const isPinnedMemo = reuse(prev?.isPinned, [pinned], () => makeIsPinned(pinned))
  const isRunningMemo = reuse(prev?.isRunning, [running], () => makeIsRunning(running))
  const folderOf = folderOfMemo.value
  const isPinned = isPinnedMemo.value
  const isRunning = isRunningMemo.value

  const laneOrder = reuse(prev?.laneOrder, [searchRanks, sortKey, pinned, pinnedRank], () =>
    makeLaneOrder(searchRanks, sortKey, pinned, pinnedRank, isPinned))
  const statusMatches = reuse(prev?.statusMatches, [unread, running, recent, status.recentWindowMs, clock], () =>
    makeStatusMatches(unread, isRunning, recent, status.recentWindowMs, clock))
  const dimensions = reuse(prev?.dimensions, [
    status.active, status.paused, tags.resolved, tags.raw, filters.tagHides.resolved, filters.tagHides.raw, search.text, search.folderMatches, searchRanks,
    statusMatches.value, folderFilter.hiddenSubtree, slotFolders,
    clears.tags, clears.tagHides, clears.search, clears.status, clears.folder,
  ], () => makeDimensions(filters, statusMatches.value, searchRanks, folderOf, clears))

  // Before the filter pass, as the chip counts always were: both read the clock for a
  // peer row's recency, and this keeps the two reads in their order.
  const statusCounts = reuse(prev?.statusCounts, [rows, statusMatches.value], () => countStatus(rows, statusMatches.value))
  const filteredSlots = reuse(prev?.filteredSlots, [rows, dimensions.value, laneOrder.value, frozen], () =>
    frozenList ?? filterRows(rows, dimensions.value, laneOrder.value))
  const listNarrowed = dimensions.value.some(d => d.narrows !== null && d.narrows())
  const revealBlockingFilters = reuse(prev?.revealBlockingFilters, [dimensions.value, filteredSlots.value], () =>
    makeRevealRegistry(dimensions.value, filteredSlots.value))

  const isRowFolderHidden = reuse(prev?.isRowFolderHidden, [folderFilter.active, folderFilter.hiddenSubtree, slotFolders], () =>
    makeIsRowFolderHidden(folderFilter.active, folderFilter.hiddenSubtree, folderOf))
  const hidden = isRowFolderHidden.value
  // The per-machine split: a crew group takes its rows out of the `Local` lanes, in
  // the same filtered order.
  const crewRows = reuse(prev?.crewRows, [crewGroups, filteredSlots.value], () =>
    groupByCrew(crewGroups, filteredSlots.value))
  const main = reuse(prev?.main, [crewRows.value, filteredSlots.value], () => (crewRows.value.size === 0
    ? filteredSlots.value
    : filteredSlots.value.filter(slot => !crewRows.value.has(crewOf(slot) ?? ''))))
  const crew = reuse(prev?.crew, [crewRows.value, hidden], () => hideInCrews(crewRows.value, hidden))
  const crewRowCount = reuse(prev?.crewRowCount, [crew.value], () =>
    [...crew.value.values()].reduce((n, list) => n + list.length, 0))
  const lane = main.value
  const flat = reuse(prev?.flat, [lane, folderFilter.active, hidden], () =>
    (folderFilter.active ? lane.filter(slot => !hidden(slot)) : lane))
  const ungrouped = reuse(prev?.ungrouped, [lane, slotFolders], () =>
    lane.filter(slot => !folderOf(slot)))
  const board = reuse(prev?.board, [lane, hidden], () =>
    lane.filter(slot => !isPeerRow(slot) && !hidden(slot)))
  const boardPeerCount = reuse(prev?.boardPeerCount, [lane], () =>
    lane.filter(isPeerRow).length)
  const folderTree = reuse(prev?.folderTree, [lane, slotFolders], () =>
    makeFolderTree(lane, folderOf))

  const result: SidebarRows = {
    filteredSlots: filteredSlots.value,
    listNarrowed,
    revealBlockingFilters: revealBlockingFilters.value,
    statusCounts: statusCounts.value,
    laneOrder: laneOrder.value,
    isRowFolderHidden: hidden,
    lanes: {
      main: lane,
      crew: crew.value,
      crewRowCount: crewRowCount.value,
      flat: flat.value,
      ungrouped: ungrouped.value,
      board: board.value,
      boardPeerCount: boardPeerCount.value,
    },
    folderTree: folderTree.value,
    folderOf,
    isPinned,
    isRunning,
    staleSplit: makeStaleSplit(listNarrowed, sortKey, clock),
    views: makeViews({ pinned, pinnedRank, unread, isRunning, searchRanks }),
  }
  memosOf.set(result, {
    folderOf: folderOfMemo, isPinned: isPinnedMemo, isRunning: isRunningMemo,
    laneOrder, statusMatches, dimensions, statusCounts, filteredSlots, revealBlockingFilters,
    isRowFolderHidden, crewRows, main, crew, crewRowCount, flat, ungrouped, board, boardPeerCount, folderTree,
  })
  return result
}

/** One placement's view. The origin is checked once, here: `local` is the row's local
 *  key or null, and every local key-indexed value below is read only through it. */
function rowView(slot: Slot, placement: RowPlacement, scene: RowScene, model: ViewModel): SessionRowView {
  const local = localKeyOf(slot)
  const identity = sessionRowIdentity(slot)
  const { renaming, revealFlash, digits } = scene.local
  const isRenaming = local !== null && renaming.key === local
  const renamingHere = isRenaming && renaming.scope === placement.scope
  const revealing = local !== null && revealFlash?.kind === 'session' && revealFlash.key === local
  return {
    slot,
    identity,
    localKey: local ?? '',
    peerId: local === null ? slot.peer_id : undefined,
    peerName: local === null ? (slot.peer_name || slot.peer_id) : undefined,
    scope: placement.scope,
    navScope: placement.navScope,
    holdContainer: placement.holdContainer,
    showDivider: placement.showDivider,
    orderStamp: placement.orderStamp,
    rowAnimEnabled: placement.rowAnimEnabled,
    conductor: placement.conductor ?? null,
    isActive: isActiveIn(scene, slot),
    isOut: local !== null && scene.local.poppedOut.has(local),
    isPinned: local !== null && model.pinned.has(local),
    isUnread: local !== null && model.unread.has(local),
    isRunning: model.isRunning(slot),
    recent: local === null ? undefined : scene.local.recentRank.get(local),
    subagentCount: local === null ? 0 : (scene.local.subagentCounts[local] || 0),
    subagentApprovalCount: local === null ? 0 : (scene.local.subagentApprovalCounts[local] || 0),
    digitBadge: local !== null && digits ? digits.get(local) : undefined,
    isRenaming,
    renamingHere,
    renameValue: renamingHere ? renaming.value : '',
    revealFlash: revealing ? (revealFlash!.fading ? 'fade' : 'flash') : null,
    // A pinned rank is a LOCAL ordering: a peer row sits outside the pinned band and
    // refuses keyboard reorder, so a key collision cannot rewrite the local pin order.
    pinnedOrderIndex: local === null ? -1 : (model.pinnedRank.get(local) ?? -1),
    pinnedReorderEnabled: !model.searchRanks && local !== null,
    opensElsewhere: local !== null && !isChatPageSurface(slot.surface ?? slot.mode)
      ? (slot.mode === 'member' && slot.agent ? `/members?member=${encodeURIComponent(slot.agent)}` : '/members')
      : null,
    shell: scene.shell,
  }
}

/** Every `SessionRowView` field. A `Record` over `keyof`, so a field added to the view
 *  without a line here is a type error rather than a field equality never compares. */
const VIEW_FIELDS: Record<keyof SessionRowView, true> = {
  slot: true, identity: true, localKey: true, peerId: true, peerName: true, scope: true, navScope: true,
  holdContainer: true, showDivider: true, orderStamp: true, rowAnimEnabled: true, conductor: true,
  isActive: true, isOut: true, isPinned: true, isUnread: true, isRunning: true, recent: true,
  subagentCount: true, subagentApprovalCount: true, digitBadge: true, isRenaming: true, renamingHere: true,
  renameValue: true, revealFlash: true, pinnedOrderIndex: true, pinnedReorderEnabled: true,
  opensElsewhere: true, shell: true,
}
const VIEW_KEYS = Object.keys(VIEW_FIELDS) as (keyof SessionRowView)[]

/** Every `ConductorRowView` field, for the same reason as `VIEW_FIELDS`. */
const CONDUCTOR_FIELDS: Record<keyof ConductorRowView, true> = {
  depth: true, childCount: true, expanded: true, aggregate: true, orphanOf: true, citesParent: true, anchorOnly: true,
}
const CONDUCTOR_KEYS = Object.keys(CONDUCTOR_FIELDS) as (keyof ConductorRowView)[]

function sameConductor(a: ConductorRowView | null, b: ConductorRowView | null): boolean {
  if (a === b) return true
  if (!a || !b) return false
  for (const key of CONDUCTOR_KEYS) {
    if (key === 'aggregate') {
      const x = a.aggregate
      const y = b.aggregate
      if (x === y) continue
      if (!x || !y || x.needsYou !== y.needsYou || x.running !== y.running) return false
    } else if (!Object.is(a[key], b[key])) {
      return false
    }
  }
  return true
}

/** Do two views render the same row? Field by field with `Object.is` (references for
 *  the slot and the shared shell), the conductor extras by value. This is the row's
 *  memo comparison, so a view rebuilt from unchanged inputs leaves the row alone. */
export function sameRowView(a: SessionRowView, b: SessionRowView): boolean {
  if (a === b) return true
  for (const key of VIEW_KEYS) {
    if (key === 'conductor') {
      if (!sameConductor(a.conductor, b.conductor)) return false
    } else if (!Object.is(a[key], b[key])) {
      return false
    }
  }
  return true
}
