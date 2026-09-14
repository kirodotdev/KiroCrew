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
}

/**
 * Build the recall list, oldest → newest: transcript prompts in order, then any
 * submitted prompt the transcript never took. Consecutive duplicates collapse.
 * An unlanded prompt goes at the TAIL — what the first ↑ press returns, so the
 * text the user just watched vanish is one keystroke away.
 *
 * Presence is decided by the send's own id, never by text or timestamp: the
 * POST expands pastes and file markers so the persisted row is spelled
 * differently, and the row's `ts` is the gateway's clock while the submission
 * is the browser's. The backend echoes the id back (see `rowIdentities`).
 *
 * Suppressed while its row is present, never deleted — an out-of-order refresh
 * would otherwise strand a prompt in neither transcript nor recall. The cost: a
 * landed prompt whose row aged out of the page is offered again.
 */
export function buildRecallHistory(
  messages: readonly RecallMessage[],
  attempted?: readonly RecallAttempt[],
): string[] {
  const out: string[] = []
  const present = new Set<string>()
  for (const m of messages) {
    if (m.role !== 'user') continue
    const text = m.rawText ?? m.content
    if (!text) continue
    const sendId = m.meta?.sendId
    if (typeof sendId === 'string' && sendId) present.add(sendId)
    if (text === out[out.length - 1]) continue
    out.push(text)
  }
  if (!attempted?.length) return out
  for (const attempt of attempted) {
    const text = attempt?.text
    if (!text) continue
    if (attempt.sendId && present.has(attempt.sendId)) continue
    if (text === out[out.length - 1]) continue
    out.push(text)
  }
  return out
}
