/**
 * ↑/↓ prompt-history browsing for the composer, as pure functions shared by
 * the textarea and Lexical composers.
 *
 * The list of sent prompts can change while the user is browsing it: an
 * optimistic bubble is replaced by the server echo, older history is loaded
 * in front of it, a queued message is sent after it. A plain array index
 * would then point at a different prompt, so the browse position is a
 * cursor that names its entry (message id where one exists, plus the text or
 * the position among entries sharing that id, since ids are not unique) and is
 * resolved against the CURRENT list on every step.
 *
 * Browsing ends as soon as the composer text stops equalling the recalled
 * entry (the user edited it, or the send pipeline cleared it). The draft
 * saved when browsing started is carried by the cursor, so it stays
 * restorable for as long as browsing lasts.
 */

import type { ChatMessage } from '../types'

/** One sent prompt; `id` is absent when the message carried no usable id. */
export type PromptHistoryItem = { text: string; id?: string }

export interface PromptHistoryCursor {
  /** Id of the recalled entry, when the list carried one. */
  id?: string
  /** Text the composer was set to by the last step. */
  text: string
  /** Entries with the same text newer than the recalled one. */
  newerSameText: number
  /**
   * Entries with the same id OLDER than the recalled one; ids are not unique.
   * Counted from the oldest because a run sharing one id grows at its newest
   * end (queued prompts drained after a rehydration share one timestamp), so
   * an append never shifts this count.
   */
  olderSameId: number
  /** Distance from the newest entry at the last step; the fallback anchor. */
  fromEnd: number
  /** Composer text saved when browsing started, restored past the newest. */
  draft: string
}

export interface PromptHistoryStep {
  /** Cursor after the step; `null` means browsing ended (draft restored). */
  cursor: PromptHistoryCursor | null
  /** Text to put in the composer. */
  text: string
}

function promptHistoryText(item: PromptHistoryItem): string {
  return item.text
}

function promptHistoryId(item: PromptHistoryItem): string | undefined {
  return item.id || undefined
}

/**
 * The cursor while the composer still shows its recalled entry, else `null`.
 * Call before every step so an edited recall is never replaced by another
 * history entry.
 */
export function livePromptHistoryCursor(
  cursor: PromptHistoryCursor | null,
  composerText: string,
): PromptHistoryCursor | null {
  return cursor && cursor.text === composerText ? cursor : null
}

/**
 * Index of the cursor's entry in `items`: by id first, then by text and its
 * position among identical entries, then by distance from the newest entry.
 * Returns -1 only for an empty list.
 */
function resolvePromptHistoryCursor(
  items: readonly PromptHistoryItem[],
  cursor: PromptHistoryCursor,
): number {
  if (!items.length) return -1
  if (cursor.id) {
    // Ids are not unique. A `ts:` id is minted from the row timestamp, and a
    // slot rehydration stamps one timestamp onto every queued row it restores,
    // so a whole run of prompts can share an id. Among the rows carrying the
    // cursor's id, a text that matches exactly one of them names it; otherwise
    // fall back to its recorded position among that id's rows, counted from
    // the oldest so rows appended to the run do not move it.
    let sameId = 0
    let byPosition = -1
    let textMatches = 0
    let byText = -1
    for (let i = 0; i < items.length; i++) {
      if (promptHistoryId(items[i]) !== cursor.id) continue
      if (promptHistoryText(items[i]) === cursor.text) {
        textMatches++
        byText = i
      }
      if (sameId === cursor.olderSameId) byPosition = i
      sameId++
    }
    if (textMatches === 1) return byText
    if (byPosition !== -1) return byPosition
  }
  let sameText = 0
  for (let i = items.length - 1; i >= 0; i--) {
    if (promptHistoryText(items[i]) !== cursor.text) continue
    if (sameText === cursor.newerSameText) return i
    sameText++
  }
  return Math.min(items.length - 1, Math.max(0, items.length - 1 - cursor.fromEnd))
}

function cursorAt(items: readonly PromptHistoryItem[], index: number, draft: string): PromptHistoryCursor {
  const text = promptHistoryText(items[index])
  const id = promptHistoryId(items[index])
  let newerSameText = 0
  for (let i = index + 1; i < items.length; i++) {
    if (promptHistoryText(items[i]) === text) newerSameText++
  }
  let olderSameId = 0
  if (id !== undefined) {
    for (let i = 0; i < index; i++) {
      if (promptHistoryId(items[i]) === id) olderSameId++
    }
  }
  return { id, text, newerSameText, olderSameId, fromEnd: items.length - 1 - index, draft }
}

/**
 * One ↑ (`older`) or ↓ (`newer`) step. `cursor` must already have been
 * passed through `livePromptHistoryCursor`. Returns `null` when the step is
 * not a history step (empty list, or ↓ while not browsing) and the key should
 * keep its native caret movement. ↑ on the oldest entry stays there.
 */
export function stepPromptHistory(
  items: readonly PromptHistoryItem[],
  cursor: PromptHistoryCursor | null,
  direction: 'older' | 'newer',
  composerText: string,
): PromptHistoryStep | null {
  if (!items.length) return null
  if (!cursor) {
    if (direction === 'newer') return null
    const next = cursorAt(items, items.length - 1, composerText)
    return { cursor: next, text: next.text }
  }
  const index = resolvePromptHistoryCursor(items, cursor)
  if (direction === 'older') {
    const next = cursorAt(items, Math.max(0, index - 1), cursor.draft)
    return { cursor: next, text: next.text }
  }
  if (index < items.length - 1) {
    const next = cursorAt(items, index + 1, cursor.draft)
    return { cursor: next, text: next.text }
  }
  return { cursor: null, text: cursor.draft }
}

function metaId(message: ChatMessage): string | undefined {
  // `sendId` is minted by the client before the optimistic bubble exists and
  // the server keeps it on the row it echoes, so it names the same prompt on
  // both sides of that swap. `meta.clientTs` is the reducer-minted identity of
  // a row that arrived without a server `ts` (a linked-channel prompt, for
  // one); it never changes once set, so it outranks a `mid` that may arrive
  // later. `mid` is the server id for rows sent elsewhere. Otherwise, `ts` is
  // stable across history prepends and appends. An echo swap that rewrites
  // `ts` also adds `mid`, so text fallback still handles it.
  const sendId = message.meta?.sendId
  if (typeof sendId === 'string' && sendId) return sendId
  const clientTs = message.meta?.clientTs
  if (typeof clientTs === 'string' && clientTs) {
    // Some paths copy `ts` into `clientTs`; keep that row's id identical to
    // the `ts:` form it had before the copy so the cursor does not lose it.
    return clientTs === message.ts ? `ts:${clientTs}` : clientTs
  }
  const mid = message.meta?.mid
  if (typeof mid === 'string' && mid) return mid
  if (typeof message.ts === 'string' && message.ts) return `ts:${message.ts}`
  return undefined
}

/**
 * The user's sent prompts, oldest first, with consecutive identical prompts
 * collapsed into one entry (the oldest of the run keeps its id).
 */
export function promptHistoryFromMessages(messages: readonly ChatMessage[]): PromptHistoryItem[] {
  const out: PromptHistoryItem[] = []
  for (const m of messages) {
    if (m.role !== 'user') continue
    const text = m.rawText ?? m.content
    if (!text || text === out[out.length - 1]?.text) continue
    const id = metaId(m)
    out.push(id ? { text, id } : { text })
  }
  return out
}

/** Element-wise equality of two prompt-history lists (text and id). */
export function samePromptHistory(
  a: readonly PromptHistoryItem[],
  b: readonly PromptHistoryItem[],
): boolean {
  return a.length === b.length && a.every((item, i) =>
    promptHistoryText(item) === promptHistoryText(b[i]) && promptHistoryId(item) === promptHistoryId(b[i]))
}
