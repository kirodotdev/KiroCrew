/**
 * Prompt history for the composer's ↑/↓ recall.
 *
 * The transcript is the obvious source and was for a long time the only one,
 * which made recall unable to offer any prompt that never reached it. That is
 * the case the user needs it MOST: the composer is cleared at submission, so
 * once a send is lost — a POST that never arrived, an optimistic bubble dropped
 * by a wholesale refresh, a bubble appended to a slot the user is not looking
 * at — the transcript has no row and the composer has no text, and the prompt
 * exists nowhere in the UI. Merging the submitted-prompt record back in gives
 * recall something to return in exactly that case.
 */

import type { PasteBlock } from '../utils/pasteTokens'

/** Minimal message shape this builder needs. `ChatMessage` satisfies it
 *  structurally, so callers pass their live messages and tests can build tiny
 *  literals without every ChatMessage field. */
export type RecallMessage = {
  role: string
  content?: string
  rawText?: string
  meta?: { sendId?: unknown }
}

/** One submitted prompt, carried with the id of the send that submitted it.
 *  Structurally satisfied by the store's `SendAttempt`. */
export type RecallAttempt = {
  text: string
  sendId: string
  files?: readonly string[]
  pastes?: readonly PasteBlock[]
}

/** A recall list position: the text ↑ yields, and the attempt it came from when
 *  it came from one. `attempt` is null for a prompt read off the transcript,
 *  which has no sidecars to restore. */
export type RecallEntry = {
  text: string
  attempt: RecallAttempt | null
}

/**
 * Build the recall list, oldest → newest: transcript prompts in order, then any
 * submitted prompt the transcript never took. Consecutive duplicates collapse,
 * and an unlanded prompt goes at the TAIL, one ↑ from the text the user just
 * watched vanish.
 *
 * Presence is decided by the send's own id, never by text or timestamp: the POST
 * re-spells the persisted row, and its `ts` is the gateway's clock while the
 * submission is the browser's. Suppressed while that row is present but never
 * deleted, so an out-of-order refresh cannot strand a prompt in neither place —
 * at the cost of re-offering a landed prompt whose row aged out. A collapse
 * REPLACES the entry it lands on, because retrying a lost send is the ordinary
 * shape here and the repeat usually holds the only sidecars: dropping it offered
 * the text with the previous send's attachments, or with none at all.
 */
export function buildRecallEntries(
  messages: readonly RecallMessage[],
  attempted?: readonly RecallAttempt[],
): RecallEntry[] {
  const out: RecallEntry[] = []
  const tail = () => out[out.length - 1]?.text
  const present = new Set<string>()
  for (const m of messages) {
    if (m.role !== 'user') continue
    const text = m.rawText ?? m.content
    if (!text) continue
    const sendId = m.meta?.sendId
    if (typeof sendId === 'string' && sendId) present.add(sendId)
    if (text === tail()) continue
    out.push({ text, attempt: null })
  }
  if (!attempted?.length) return out
  for (const attempt of attempted) {
    const text = attempt?.text
    if (!text) continue
    if (attempt.sendId && present.has(attempt.sendId)) continue
    // Replace, never drop: the newest record of a repeated prompt owns its
    // sidecars (see the collapse paragraph above).
    const entry = { text, attempt }
    if (text === tail()) out[out.length - 1] = entry
    else out.push(entry)
  }
  return out
}

