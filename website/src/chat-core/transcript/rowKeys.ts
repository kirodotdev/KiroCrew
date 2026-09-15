/**
 * Row identity for a virtualized transcript: the pure key builders every
 * host that runs `useVirtualChat` over `DisplayItem`s needs — the virtualizer
 * / HeightCache key per row (`virtualKeyFor`, list-unique via `uniqueRowKeys`)
 * and the two scroll-anchor identities (`stableAnchorIdFor` on the tail,
 * `anchorAltIdFor` on the lead). Extracted from the main chat page
 * (chat-core P5-e) so ChatPane, SideChat and ChatEmbed key their rows exactly
 * as ChatPage does; ChatPage's own module re-exports these names, so its
 * imports and the tests that pin them are unchanged.
 *
 * Pure functions with the per-message identity injected (`msgKey`: clientTs →
 * ts → minted id, never the array index — see `useStableMessageKey`), so the
 * regroup- and steer-reconcile-stability guarantees stay unit-testable.
 */
import type { ChatMessage } from '../../types'
import { TURN_OPENER_ROLES } from '../../pages/chat/groupDisplayItems'
import type { DisplayItem, TurnItem } from '../../pages/chat/types'

export function msgIdentityKey(m: ChatMessage, msgKey: (m: ChatMessage) => string): string {
  const mid = m.meta?.mid
  return typeof mid === 'string' && mid ? `${msgKey(m)}~${mid}` : msgKey(m)
}

/** Stable key for a single TurnItem — the leading row of a turn OR a top-level
 *  single/group. A `single` and the `turn` it leads resolve to the SAME key so
 *  a mid-stream regroup (single promoted into a grouped turn once it gains
 *  working steps) does NOT change the row's virtual key → no remount / silent
 *  re-measure. `msgKey` supplies the per-message identity (clientTs → ts →
 *  minted id; never the array index — see stableMsgKey). Groups key on their
 *  FIRST MESSAGE's identity, never `startIdx`: a prepend (history backfill)
 *  renumbers every array index but leaves message identities intact, so a
 *  group-led row keeps its key — and with it its cached height, DOM node, and
 *  scroll anchor — across the shift. The index key this replaces was unique by
 *  construction, so group keys go through `msgIdentityKey` to keep that
 *  property across same-tick `ts` ties.
 *
 *  `msgs` is non-empty by construction (both producers emit a group only under
 *  `if (group.length)`), but the type allows `[]` and this is a public export —
 *  degrade to the index rather than throwing inside `msgKey`. */
export function turnLeadKey(it: TurnItem, msgKey: (m: ChatMessage) => string): string {
  if (it.kind === 'single') return `row-${msgKey(it.msg)}`
  const lead = it.msgs[0]
  return lead ? `grp-${msgIdentityKey(lead, msgKey)}` : `grp-idx-${it.startIdx}`
}

/** Virtualizer / HeightCache key for a display row. Pure (identity injected)
 *  so the steer-reconcile-stability and regroup-stability guarantees are
 *  unit-testable. A `turn` inherits the key of its leading item so promoting a
 *  single into a turn (and vice-versa) keeps the row identity — and thus its
 *  cached height and DOM node — stable. */
/** Anchor identity that survives a prepend's key reshuffle: the TAIL message's
 *  identity. A page landing regroups older messages into the top turn's HEAD —
 *  renaming its lead-derived display key — but a turn's newest message is
 *  untouched by content arriving before it, so an anchor held by the tail
 *  resolves across the landing and its compensation is not dropped. Falls back
 *  to positional markers only for degenerate empty rows, mirroring
 *  virtualKeyFor's own fallbacks. */
/** SECOND anchor identity for a display row: its LEAD message, `l-` prefixed so
 *  it shares no vocabulary with a tail id and the two can never cross-match.
 *
 *  Exists because neither end of a turn is stable on its own. `stableAnchorIdFor`
 *  takes the tail, which a page landing cannot rename -- but a turn STILL
 *  STREAMING gains messages at that end, so its tail id changes under a reader
 *  who has not moved. The lead is untouched by appends (and by a single being
 *  promoted into the turn it leads, which keeps the first message). Persisting
 *  both lets the restore match whichever end survived. */
export function anchorAltIdFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
): string {
  const leadOf = (t: TurnItem): ChatMessage | null =>
    t.kind === 'single' ? t.msg : (t.msgs[0] ?? null)
  let lead: ChatMessage | null = null
  if (it.kind === 'turn') {
    const first = it.items[0]
    lead = first ? leadOf(first) : null
  } else {
    lead = leadOf(it)
  }
  if (!lead) return `alt-empty-${index}`
  return `l-${msgIdentityKey(lead, msgKey)}`
}

export function stableAnchorIdFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
): string {
  const tailOf = (t: TurnItem): ChatMessage | null =>
    t.kind === 'single' ? t.msg : (t.msgs[t.msgs.length - 1] ?? null)
  let tail: ChatMessage | null = null
  if (it.kind === 'turn') {
    const last = it.items[it.items.length - 1]
    tail = last ? tailOf(last) : null
  } else {
    tail = tailOf(it)
  }
  if (!tail) return `anchor-empty-${index}`
  return `a-${msgIdentityKey(tail, msgKey)}`
}

export function virtualKeyFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
  isTrailing = false,
): string {
  if (it.kind === 'turn') {
    const first = it.items[0]
    if (!first) return `turn-empty-${index}`
    // A turn WITH its opening prompt keys on that lead: the lead never
    // changes once the prompt is loaded, and the trailing turn's tail grows
    // every stream tick (tail-keying it would remount per token).
    //
    // A HEADLESS turn -- the topmost boundary turn of a partially loaded
    // transcript, whose opening prompt is still in an unloaded older page --
    // keys on its TAIL instead. Every older-page landing feeds that turn's
    // HEAD, so a lead-derived key renamed the row per landing: React sees a
    // new element, unmounts the giant row and remounts it (Pierre surfaces
    // visibly "reload", and the height cache line is orphaned) -- once per
    // walk wave, which is the refresh-then-walk bounce. The tail is
    // untouched by content arriving above it, so the key holds across the
    // whole walk; when the opening prompt finally lands the key flips to
    // the lead ONCE (one remount, its height migrated by the departure
    // rename pass). A headless turn is by construction not the trailing
    // streaming turn in any live session older than one page, and a fresh
    // session's single turn has its prompt loaded, so tail growth cannot
    // re-key it in practice.
    // ...with one exception: the TRAILING turn is never tail-keyed, even
    // headless. A refresh into a giant in-flight turn loads a window that
    // is entirely that one turn -- headless AND growing at the tail, where
    // a tail key would remount it per stream tick. Lead-keying it merely
    // keeps the pre-fix behavior (renamed per landing) for the one turn
    // that is anchored to the viewport bottom anyway.
    // ...and only at INDEX 0: the walk feeds the head of the TOPMOST row
    // alone. A mid-list turn without an opener lead (an interim fold, a
    // single mid-stream promoting into the turn it leads) keeps the #253
    // lead-key contract -- its head is bounded by settled content, so
    // landings cannot rename it, and tail-keying it would itself remount
    // the row at every fold boundary.
    const leadMsg = first.kind === 'single' ? first.msg : first.msgs[0]
    // `complete === false` is the running-turn marker (applyRunningState):
    // an in-flight turn grows at its tail and must never key on it,
    // whatever its position -- the semantic twin of the positional
    // isTrailing guard, and the one that holds when the loaded window IS
    // one giant in-flight turn (refresh mid-turn: index 0 AND trailing).
    if (index === 0 && !isTrailing && it.complete !== false && leadMsg && !TURN_OPENER_ROLES.has(leadMsg.role)) {
      const last = it.items[it.items.length - 1]
      const tail = last
        ? (last.kind === 'single' ? last.msg : (last.msgs[last.msgs.length - 1] ?? null))
        : null
      if (tail) return `hlt-${msgIdentityKey(tail, msgKey)}`
    }
    return turnLeadKey(first, msgKey)
  }
  return turnLeadKey(it, msgKey)
}

/** Virtualizer keys for the WHOLE display list, with a collision tie-break.
 *
 *  `virtualKeyFor` is not unique across the list: a `single` keys on
 *  `msgKey` alone (`row-<ts>`), and a coarse OS clock can stamp two rows
 *  appended in one tick with the same `ts` — the exact hazard `msgIdentityKey`
 *  closes for group leads. Two rows sharing one key reach React as duplicate
 *  siblings (one is silently dropped from the DOM — content visibly missing)
 *  and share one HeightCache slot (each re-measure of either row reprices the
 *  other, oscillating the spacers). Same failure from an overlapping older
 *  page whose rows lack the `meta.mid` the prepend dedup keys on.
 *
 *  The tie-break is positional among COLLIDERS ONLY: the first occurrence
 *  keeps the bare key — so the common case is byte-identical to
 *  `virtualKeyFor` and every cached height, DOM node, and scroll anchor keyed
 *  before this pass survives — and each later duplicate gets an occurrence
 *  suffix. Deterministic for a given list order, so keys are stable across
 *  re-renders. An insert BEFORE a collider shifts which physical row holds
 *  the bare key: those rows remount (a `~#N` height or scroll anchor
 *  persisted under the old occupant can also go stale until re-measured) —
 *  bounded to rows that previously rendered broken (dropped sibling), and
 *  strictly better than that render.
 *
 *  NOT folded into `virtualKeyFor`: uniqueness is a property of the list, not
 *  of one row, and a per-row `~mid` suffix instead would rename every
 *  streamed/optimistic row (which lacks `mid`) at the post-turn `refreshSlot`
 *  rebuild (which carries it) — a mass remount per turn end. */
/** Tie-break suffix for colliding virtualizer keys. Key plumbing only — the
 *  string never renders as user-visible text. */
const DUP_KEY_SUFFIX = '~#'

export function uniqueRowKeys(
  items: readonly DisplayItem[],
  msgKey: (m: ChatMessage) => string,
): string[] {
  const seen = new Map<string, number>()
  return items.map((it, i) => {
    const base = virtualKeyFor(it, i, msgKey, i === items.length - 1)
    const n = seen.get(base)
    if (n === undefined) {
      seen.set(base, 1)
      return base
    }
    // The suffixed candidate is re-checked against `seen` too: a NATURAL key
    // can spell `<base>~#1` (msgKey passes through arbitrary meta), so
    // emitting the suffix unchecked would reintroduce the duplicate this
    // function exists to remove.
    let count = n
    let candidate = `${base}${DUP_KEY_SUFFIX}${count}`
    while (seen.has(candidate)) {
      count++
      candidate = `${base}${DUP_KEY_SUFFIX}${count}`
    }
    seen.set(base, count + 1)
    seen.set(candidate, 1)
    return candidate
  })
}
