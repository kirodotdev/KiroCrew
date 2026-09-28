import { createSelector } from '@reduxjs/toolkit'
import type { ChatMessage, ToolActivity } from '../../types'

/**
 * Per-slot index over a transcript's messages + tool log, built ONCE per
 * (messages, toolLog) array-identity pair and shared by every mounted
 * ToolCallLine row in that slot.
 *
 * Why this exists: each ToolCallLine used to run O(messages + toolLog)
 * reverse scans INSIDE its useAppSelector bodies — a full permission walk, a
 * 🚫-sibling walk, and a toolLog reverse scan — and useAppSelector re-runs on
 * every store dispatch, per mounted row. With N rows mounted that is N full
 * walks per dispatch. The index converts every one of those scans into an
 * O(1) map lookup; the single O(messages + toolLog) build runs only when one
 * of the two arrays actually changes identity.
 *
 * Memoization: reselect 5's default weakMapMemoize keys the cache on the
 * input array identities via WeakMaps, so results for different slots coexist
 * (no LRU thrash between the active slot and split-view panes) and an index
 * is released as soon as the store releases its source arrays — nothing here
 * pins old messages arrays alive.
 */
export interface ToolRowIndex {
  /** Latest tool-log entry per tool_call_id (last write wins — mirrors the
   *  reverse scan's "first match from the end"). */
  logById: Map<string, ToolActivity>
  /** Id-bearing tool-log entries, newest first, for id-less historical rows
   *  whose only join key is a label-substring match on entry text. */
  idBearingLogDesc: ToolActivity[]
  /** Latest permission message per tool_call_id — the reverse permission scan
   *  only ever consulted the newest one. */
  lastPermById: Map<string, ChatMessage>
  /** tool_call_ids that have at least one UNRESOLVED permission message. */
  pendingPermIds: Set<string>
  /** All tool-role messages per tool_call_id, in transcript order — the
   *  🚫-deny-sibling scan walks only this row's own id group. */
  toolMsgsById: Map<string, ChatMessage[]>
  /** The transcript's newest tool-role message (owns the wait countdown). */
  lastToolMsg: ChatMessage | undefined
}

/** Build-count probe: lets tests assert the index is built once per
 *  (messages, toolLog) identity change, not once per mounted row. */
function buildToolRowIndex(messages: ChatMessage[], toolLog: ToolActivity[]): ToolRowIndex {
  const logById = new Map<string, ToolActivity>()
  const idBearingLogDesc: ToolActivity[] = []
  for (const e of toolLog) {
    if (e.type !== 'tool' || !e.tool_call_id) continue
    logById.set(e.tool_call_id, e)
    idBearingLogDesc.push(e)
  }
  idBearingLogDesc.reverse()
  const lastPermById = new Map<string, ChatMessage>()
  const pendingPermIds = new Set<string>()
  const toolMsgsById = new Map<string, ChatMessage[]>()
  let lastToolMsg: ChatMessage | undefined
  for (const m of messages) {
    const id = m.meta?.tool_call_id as string | undefined
    if (m.role === 'permission') {
      if (!id) continue
      lastPermById.set(id, m)
      if (!m.meta?.resolved) pendingPermIds.add(id)
    } else if (m.role === 'tool') {
      lastToolMsg = m
      if (!id) continue
      const arr = toolMsgsById.get(id)
      if (arr) arr.push(m)
      else toolMsgsById.set(id, [m])
    }
  }
  return { logById, idBearingLogDesc, lastPermById, pendingPermIds, toolMsgsById, lastToolMsg }
}

/** Memoized on the two array identities (weakMapMemoize — see module doc). */
export const selectToolRowIndex = createSelector(
  [(messages: ChatMessage[]) => messages, (_messages: ChatMessage[], toolLog: ToolActivity[]) => toolLog],
  buildToolRowIndex,
)

// Per-index cache of id-less label lookups. The historical fallback matches a
// row to the newest id-bearing log entry whose text is a SUBSTRING of the
// row's label — substring matching cannot be a map lookup, so the linear scan
// survives here, but it runs once per (index, label) pair instead of once per
// dispatch per row. WeakMap-keyed on the index so it dies with it.
const idLessLookups = new WeakMap<ToolRowIndex, Map<string, ToolActivity | null>>()

/**
 * Resolve the tool-log entry backing a row: by tool_call_id when the row has
 * one, else by the label-substring fallback for id-less historical rows.
 * Returns undefined when no entry matches (the caller falls to the
 * historical-message branch). Semantics match the old reverse scan exactly:
 * newest entry wins, and the id-less path only considers id-bearing entries.
 */
export function lookupLogEntry(index: ToolRowIndex, toolCallId: string | undefined, label: string): ToolActivity | undefined {
  if (toolCallId) return index.logById.get(toolCallId)
  let cache = idLessLookups.get(index)
  if (!cache) { cache = new Map(); idLessLookups.set(index, cache) }
  const hit = cache.get(label)
  if (hit !== undefined) return hit ?? undefined
  let found: ToolActivity | null = null
  for (const e of index.idBearingLogDesc) {
    if (label.includes(e.text)) { found = e; break }
  }
  cache.set(label, found)
  return found ?? undefined
}

/**
 * The 🚫 deny-sibling for a pill: the newest tool message sharing the pill's
 * tool_call_id whose content starts with 🚫, looking only ABOVE the pill's own
 * message (the old scan walked from the end and stopped at the pill itself).
 * O(k) over the id's own sibling group — in practice 2 messages.
 */
export function denySiblingContent(index: ToolRowIndex, toolCallId: string | undefined, ownMessage: ChatMessage): string {
  if (!toolCallId) return ''
  const siblings = index.toolMsgsById.get(toolCallId)
  if (!siblings) return ''
  for (let j = siblings.length - 1; j >= 0; j--) {
    const m = siblings[j]
    if (m.content.startsWith('🚫')) return m.content
    // The pill's own 🔧 message reached without a 🚫 sibling above it — any
    // earlier match would predate this call; stop scanning.
    if (m === ownMessage) break
  }
  return ''
}
