/**
 * Assistant-role system notices the backend injects into the feed -- status
 * reports (auto-compaction, session reload confirmations, session-switch
 * boundary rows, Shared Crew memory changes, Task Runner notices in the chat
 * that started the run), not real turns.
 *
 * Every scan that walks backward for "the assistant's last word" (follow-up
 * [OPTIONS:] derivation, the continuable-thread checks) must skip these, or a
 * trailing notice hides the buttons / state of the genuine turn before it.
 * One predicate shared by all scan sites so a new notice kind cannot be added
 * to one scan and forgotten in another.
 */
const SYSTEM_NOTICE_KINDS: ReadonlySet<string> = new Set([
  'compaction',
  'session_reload',
  'session_switch',
  'memory_change',
  'taskrunner_notice',
  // Automatic resume paused for this chat: its per-chat budget is spent (restart_resume).
  // No apostrophes in these comments: test_dashboard_chat.py reads the kinds
  // out of this literal with a single-quote regex.
  'auto_resume_paused',
])

export function isSystemNoticeKind(kind: string | undefined): boolean {
  return !!kind && SYSTEM_NOTICE_KINDS.has(kind)
}
