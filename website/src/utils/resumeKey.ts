/** The member-thread namespace, `dashboard:member-<slug>`, as the gateway mints it. */
const MEMBER_THREAD_KEY_PREFIX = 'dashboard:member-'

/**
 * The filename stem the gateway files a session's transcript under
 * (`history_files._safe_key`): every character outside `[A-Za-z0-9_.-]` becomes
 * `_`. ASCII only, as dashboard slot keys are: the gateway's `\w` also keeps
 * non-ASCII letters, which this folds.
 */
function transcriptStem(sessionKey: string): string {
  return sessionKey.replace(/[^\w.-]/g, '_')
}

/**
 * The key `resumeFromHistory` names a session by: its transcript stem, the key
 * its History row carries, so the reopened row leaves Older sessions.
 */
export function resumeKeyFor(sessionKey: string): string {
  // chat_api/resume.py admits a member thread only when the requested key equals `dashboard:member-<slug>` as a raw string.
  if (sessionKey.startsWith(MEMBER_THREAD_KEY_PREFIX)) return sessionKey
  return transcriptStem(sessionKey)
}
