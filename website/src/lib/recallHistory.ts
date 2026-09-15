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
}

/**
 * Build the recall list, oldest → newest.
 *
 * Transcript prompts first, in transcript order, then any submitted prompt the
 * transcript never took. Consecutive duplicates collapse, matching a shell.
 *
 * An unlanded prompt goes at the TAIL rather than at its submission position:
 * it has no position in a transcript it never entered, and the tail is what the
 * first ↑ press returns — so the text the user just watched vanish is one
 * keystroke away. A prompt that DID land is already present and is skipped, so
 * a successful send is never offered twice.
 */
export function buildRecallHistory(
  messages: readonly RecallMessage[],
  attempted?: readonly string[],
): string[] {
  const out: string[] = []
  for (const m of messages) {
    if (m.role !== 'user') continue
    const text = m.rawText ?? m.content
    if (!text || text === out[out.length - 1]) continue
    out.push(text)
  }
  if (!attempted?.length) return out
  const seen = new Set(out)
  for (const text of attempted) {
    if (!text || seen.has(text)) continue
    out.push(text)
    seen.add(text)
  }
  return out
}
