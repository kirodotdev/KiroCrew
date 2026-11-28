import type { ChatMessage } from '../types'

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
])

export function isSystemNoticeKind(kind: string | undefined): boolean {
  return !!kind && SYSTEM_NOTICE_KINDS.has(kind)
}

/** The tag lives on `kind` for a row that arrived live over the websocket and
 *  on `meta.kind` for one reloaded from disk. */
export function noticeKindOf(m: Pick<ChatMessage, 'kind' | 'meta'>): string | undefined {
  return m.kind ?? (m.meta?.kind as string | undefined)
}

/** Any assistant-role system notice the gateway injects (compaction, session
 *  reload). One predicate shared with the last-real-message scans, so a kind
 *  those scans skip can never fall through to the reply bubble. */
export function isSystemNoticeRow(m: Pick<ChatMessage, 'role' | 'kind' | 'meta'>): boolean {
  return m.role === 'assistant' && isSystemNoticeKind(noticeKindOf(m))
}
