/**
 * The state behind Find in chat (`useMessageSearch`): the matches, which one is
 * current, and which find action last asked to move the reader to it.
 *
 * A match is named by its message's row key and its occurrence in that message,
 * never by its place in the list. The row key follows the rule the transcript
 * keys its rows by (`useStableMessageKey`), so the current match stays the same
 * match while rows arrive around it: an older page landing above it, a reply
 * arriving below it, a tool step that folds a reply above it into reasoning.
 *
 * Moving the reader is its own signal, `reveal`: a number that only a find
 * action hands out (a new term or case, a step, Home or End, a result click).
 * Rows arriving never hand one out, so they never move the reader.
 */
import type { ChatMessage } from '../types'
import { searchableTextMemo } from '../utils/searchableText'
import { isHiddenSupersededSteer } from '../pages/chat/groupDisplayItems'
import { isHiddenReplyAt } from '../app-sdk/turnPolicyBlock'

/** One match: its message's key (`messageKeys`) and its 0-based occurrence there. */
export interface MatchId {
  key: string
  occ: number
}

export interface FindState {
  /** Every match the last search found, in reading order. */
  ids: readonly MatchId[]
  /** Where the current match is in `ids`: -1 exactly when `ids` is empty. */
  at: number
  /** The term and case `ids` were found for. */
  term: string
  caseSensitive: boolean
  /** The find action that asked to move the reader to the current match, or 0
   *  when the current match became current without one. */
  reveal: number
  /** The last number handed out as a `reveal`. Never reset, so a number spent
   *  before a close or a session switch is never pending again. */
  seq: number
}

export const NO_FIND: FindState = { ids: [], at: -1, term: '', caseSensitive: false, reveal: 0, seq: 0 }

/** Each row's key (`messageKeys`) to its index in the messages list. */
export type RowIndex = ReadonlyMap<string, number>

export type FindAction =
  /** A search over the rows as they are now has finished. */
  | { type: 'found'; ids: readonly MatchId[]; term: string; caseSensitive: boolean }
  /** Enter or Shift+Enter, or an arrow, over the matches whose rows `rows` holds. */
  | { type: 'step'; delta: 1 | -1; rows: RowIndex }
  /** Home, End or a result click: match `index` of those whose rows `rows` holds. */
  | { type: 'goto'; index: number; rows: RowIndex }
  | { type: 'clear' }

// Key plumbing only, never shown: a separator no row key can contain.
const SEP = '\u0000'

/**
 * Each message's key for the find, in list order: its row key (clientTs, else
 * ts, else an id minted per message object), with its place among earlier
 * messages that share that key, since a coarse clock can stamp two rows alike.
 * shortcut: messages that share a key are told apart by their order, so a page
 * boundary that splits such a pair swaps their keys when the older one lands;
 * key them by `meta.mid` too once every row carries one from the start.
 */
export function messageKeys(messages: readonly ChatMessage[], keyOf: (m: ChatMessage) => string): string[] {
  const seen = new Map<string, number>()
  return messages.map(m => {
    const key = keyOf(m)
    const n = seen.get(key) ?? 0
    seen.set(key, n + 1)
    return n === 0 ? key : `${key}${SEP}${n}`
  })
}

/** Every occurrence of `term` in the rows the transcript draws as messages. */
export function findMatchIds(messages: ChatMessage[], keys: readonly string[], term: string, caseSensitive: boolean): MatchId[] {
  const needle = caseSensitive ? term : term.toLowerCase()
  const result: MatchId[] = []
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (m.role !== 'user' && m.role !== 'assistant') continue
    // A superseded steer is not drawn; its successor row carries the same
    // text and is the hit the reader can land on.
    if (isHiddenSupersededSteer(messages, i)) continue
    if (m.role === 'assistant' && isHiddenReplyAt(m, messages, i)) continue
    // Skip assistant reasoning segments collapsed inside turns
    if (m.role === 'assistant') {
      const next = messages[i + 1]
      if (next && next.role === 'tool') continue
    }
    const haystack = caseSensitive ? searchableTextMemo(m) : searchableTextMemo(m).toLowerCase()
    let pos = 0
    let occ = 0
    while (true) {
      const idx = haystack.indexOf(needle, pos)
      if (idx === -1) break
      result.push({ key: keys[i], occ })
      occ++
      pos = idx + needle.length
    }
  }
  return result
}

const nameOf = (id: MatchId) => `${id.occ}${SEP}${id.key}`

/**
 * Where the current match `prev[at]` is in `next`: the same match when `next`
 * still has it. Otherwise the next one in reading order, which is the first
 * match after the last one before it that `next` still has, else the last one.
 * With no current match, the first one.
 */
export function carryCurrent(prev: readonly MatchId[], at: number, next: readonly MatchId[]): number {
  if (!next.length) return -1
  if (at < 0) return 0
  const where = new Map(next.map((id, i) => [nameOf(id), i]))
  for (let i = at; i >= 0; i--) {
    const found = where.get(nameOf(prev[i]))
    if (found === undefined) continue
    return i === at ? found : Math.min(found + 1, next.length - 1)
  }
  return 0
}

/** The places in `ids` of the matches whose rows `rows` holds, ascending. */
export function livePlaces(ids: readonly MatchId[], rows: RowIndex): number[] {
  const live: number[] = []
  ids.forEach((id, i) => { if (rows.has(id.key)) live.push(i) })
  return live
}

/** The current match's position among `live` (from `livePlaces`): its own,
 *  else the next one in reading order, else the last. -1 when `live` is empty. */
export function livePosition(at: number, live: readonly number[]): number {
  if (!live.length) return -1
  const pos = live.findIndex(i => i >= at)
  return pos < 0 ? live.length - 1 : pos
}

export function findReducer(state: FindState, action: FindAction): FindState {
  switch (action.type) {
    case 'found': {
      const { ids, term, caseSensitive } = action
      if (term !== state.term || caseSensitive !== state.caseSensitive) {
        // A new search: the reader keeps their match number, clamped to the
        // new count, and is moved to it.
        const at = ids.length ? Math.min(Math.max(state.at, 0), ids.length - 1) : -1
        if (at < 0) return { ...state, ids, at, term, caseSensitive, reveal: 0 }
        return { ids, at, term, caseSensitive, reveal: state.seq + 1, seq: state.seq + 1 }
      }
      // The same search over rows that changed: the current match stays itself,
      // and so does the reader. When its row is gone, the next match takes over
      // without moving the reader.
      const at = carryCurrent(state.ids, state.at, ids)
      const kept = at >= 0 && state.at >= 0 && nameOf(ids[at]) === nameOf(state.ids[state.at])
      return { ...state, ids, at, reveal: kept ? state.reveal : 0 }
    }
    case 'step':
    case 'goto': {
      const live = livePlaces(state.ids, action.rows)
      if (!live.length) return state
      const pos = action.type === 'goto'
        ? Math.max(0, Math.min(action.index, live.length - 1))
        : (livePosition(state.at, live) + action.delta + live.length) % live.length
      const seq = state.seq + 1
      return { ...state, at: live[pos], reveal: seq, seq }
    }
    case 'clear':
      return !state.ids.length && !state.term ? state : { ...NO_FIND, seq: state.seq }
  }
}
