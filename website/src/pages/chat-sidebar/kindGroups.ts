/**
 * Pure helpers for the ChatSidebar "Group by kind" view option.
 *
 * The session list mixes three kinds of chat: Crew Mode roots (conductors),
 * ordinary chats, and chats that belong to a crewmate. When the option is on,
 * the list splits into three headed sections in that order -- the flat lane's
 * one list, and the tree lane's top-level unfiled list (a folder body keeps its
 * own order). It is VIEW ONLY: a session's folder, tags, stored order and
 * transcript are untouched -- the only thing that changes is how the
 * already-filtered, already-sorted list is bucketed for display, and the
 * within-section order is exactly the order the list already had.
 *
 * The kind of a session reuses the SAME signals the rest of the sidebar reads,
 * rather than a new classifier:
 *  - Crew Mode     -> `crew_mode_root` (the Crew Mode filter chip's own signal,
 *                     `rows.ts` `crew: slot => !!slot.crew_mode_root`);
 *  - From crewmates -> `isCrewmateChat` (the picker/BroadcastPanel rule, from
 *                     the slot's `agent_kind`, falling back to the roster);
 *  - Normal        -> everything else.
 *
 * Kept render-free so the ordering and classification are unit-testable without
 * a component, mirroring the sibling `staleCollapse` / `recentWindow` modules.
 */
import { isCrewmateChat } from '../../lib/agentMemorySource'
import type { Slot } from './types'
import type { RenderedLane } from './lanes'

export type SessionKind = 'crew' | 'normal' | 'crewmate'

/**
 * Whether "Group by kind" is ACTIVE for a given rendered lane: on, AND the lane
 * drawn is one that paints the kind sections. Only the flat lane and the tree
 * lane group; the board (tag columns, no single list) and the conductor lane
 * (nests by lineage) never do. This is the ONE rule the menu's toggle
 * visibility, the lanes' grouping, and the manual-order drag scope all read, so
 * the drag/order math can never disagree with what is painted (a board or
 * conductor lane left `drawnOrder` bucketed would seed a kind order the lane
 * never showed). The caller still ANDs in its own "not inside a folder body"
 * scope for the tree lane, where only the unfiled list is grouped.
 */
export function kindGroupingActiveForLane(groupByKind: boolean, renderedLane: RenderedLane): boolean {
  return groupByKind && (renderedLane === 'flat' || renderedLane === 'tree')
}

/** The three sections, in the order the issue asks for them: Crew Mode first
 *  (the conductors), then Normal, then From crewmates. The grouping walks this
 *  order, so a change here is the single place that reorders the sections. */
export const KIND_ORDER: readonly SessionKind[] = ['crew', 'normal', 'crewmate']

/** i18n key per section header. One entry per `SessionKind`, so a new kind is a
 *  type error here rather than a header that renders with no label. */
export const KIND_LABEL_KEY: Record<SessionKind, string> = {
  crew: 'pages.chatSidebar.group_by_kind_crew',
  normal: 'pages.chatSidebar.group_by_kind_normal',
  crewmate: 'pages.chatSidebar.group_by_kind_crewmate',
}

/**
 * The kind of one session, from the same fields the rest of the sidebar uses.
 *
 * Crew Mode wins first: a crew root could in principle also be a crewmate's
 * chat, and the Crew Mode section is the one the issue names first. `agents` is
 * the installed roster (`selection_kind`), consulted by `isCrewmateChat` only
 * when the slot carries no `agent_kind` of its own -- the common case is a slot
 * stamped `member`, which needs no roster lookup.
 */
export function sessionKind(
  slot: Slot,
  agents: readonly { name: string; selection_kind?: string }[],
): SessionKind {
  if (slot.crew_mode_root) return 'crew'
  if (isCrewmateChat(slot.agent_kind, slot.agent ?? '', agents)) return 'crewmate'
  return 'normal'
}

/**
 * Re-bucket the flat lane's list into kind sections for display.
 *
 * The leading PINNED rows are left exactly where they are, as one contiguous
 * block at the front, so the pinned band stays a pinned band and the
 * pinned->unpinned boundary the divider is drawn at does not move. Only the
 * UNPINNED tail is grouped, stably, into `KIND_ORDER`: within one kind the rows
 * keep the order they already had, so the active sort still decides the order
 * inside each section. An empty kind contributes no rows (and so no header).
 *
 * Pure and order-preserving within each bucket; it allocates a new array and
 * never mutates the input.
 */
export function groupFlatByKind(
  list: readonly Slot[],
  isPinned: (slot: Slot) => boolean,
  kindOf: (slot: Slot) => SessionKind,
): Slot[] {
  const pinned: Slot[] = []
  const buckets: Record<SessionKind, Slot[]> = { crew: [], normal: [], crewmate: [] }
  for (const slot of list) {
    if (isPinned(slot)) pinned.push(slot)
    else buckets[kindOf(slot)].push(slot)
  }
  const out: Slot[] = [...pinned]
  for (const kind of KIND_ORDER) out.push(...buckets[kind])
  return out
}


/**
 * The same stable kind bucketing as `groupFlatByKind`, but over a flat list of
 * row KEYS (no pinned rows -- the manual-order "drawn order" already excludes
 * them). Used so the manual drag's `drawnOrder`/`drawnIndex` match the order the
 * lanes actually paint when "Group by kind" is on: a drop then lands where the
 * indicator shows, and the from/to indices are the drawn ones.
 *
 * `kindOfKey` returns the kind for a key; a key it cannot classify falls in
 * `normal`, the same default `sessionKind` uses. Order within a kind is kept.
 */
export function orderKeysByKind(
  keys: readonly string[],
  kindOfKey: (key: string) => SessionKind,
): string[] {
  const buckets: Record<SessionKind, string[]> = { crew: [], normal: [], crewmate: [] }
  for (const key of keys) buckets[kindOfKey(key)].push(key)
  const out: string[] = []
  for (const kind of KIND_ORDER) out.push(...buckets[kind])
  return out
}

/**
 * Like `orderKeysByKind`, but only the keys for which `isGroupedKey` is true are
 * re-bucketed; every other key keeps its exact position in the stream.
 *
 * This matches the tree lane, where only the top-level UNFILED rows are drawn
 * grouped and a folder body keeps its own order: the grouped keys are reordered
 * into KIND_ORDER among the stream slots they occupied, and an ungrouped
 * (foldered) key is left where it was, so the manual drag's indices line up
 * with what each container actually paints. In the flat lane `isGroupedKey` is
 * true for every key and this reduces to `orderKeysByKind`.
 */
export function orderKeysByKindScoped(
  keys: readonly string[],
  kindOfKey: (key: string) => SessionKind,
  isGroupedKey: (key: string) => boolean,
): string[] {
  const buckets: Record<SessionKind, string[]> = { crew: [], normal: [], crewmate: [] }
  for (const key of keys) if (isGroupedKey(key)) buckets[kindOfKey(key)].push(key)
  const grouped: string[] = []
  for (const kind of KIND_ORDER) grouped.push(...buckets[kind])
  let g = 0
  return keys.map(key => (isGroupedKey(key) ? grouped[g++] : key))
}
