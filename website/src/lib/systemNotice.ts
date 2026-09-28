/**
 * Assistant-role system notices the backend injects into the feed -- status
 * reports (auto-compaction, session reload confirmations), not real turns.
 *
 * Every scan that walks backward for "the assistant's last word" (follow-up
 * [OPTIONS:] derivation, the continuable-thread checks) must skip these, or a
 * trailing notice hides the buttons / state of the genuine turn before it.
 * One predicate shared by all scan sites so a new notice kind cannot be added
 * to one scan and forgotten in another.
 */
const SYSTEM_NOTICE_KINDS: ReadonlySet<string> = new Set(['compaction', 'session_reload'])

export function isSystemNoticeKind(kind: string | undefined): boolean {
  return !!kind && SYSTEM_NOTICE_KINDS.has(kind)
}
