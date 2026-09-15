/**
 * The gateway's `meta.sent_by` record -- present and well-formed -- marks a
 * user-role row that ANOTHER session authored (a peer crew member's
 * `session_send`, a worker's `send_message(session="origin")`). Three
 * consumers ask the same question and must agree: the transcript (which draws
 * such a row as a "From <sender>" card instead of the person's own bubble), the
 * roster's unread badge (which counts these rows), and the pinned-prompt band
 * (which must NOT quote one as "what the human typed").
 *
 * Shape-checked, not trusted: the frame is server data, but a partial record
 * must not count toward a badge or steal a card. The parse into a typed record
 * lives with the card (`pages/chat/SentByCard.tsx`); this is only the predicate.
 */
export function isSentByMeta(meta: unknown): boolean {
  if (!meta || typeof meta !== 'object') return false
  const rec = (meta as { sent_by?: unknown }).sent_by
  return !!rec && typeof rec === 'object'
    && typeof (rec as { session_key?: unknown }).session_key === 'string'
    && typeof (rec as { via?: unknown }).via === 'string'
}
