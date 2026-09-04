/**
 * Wire contract for the `cls` token that marks a note breadcrumb.
 *
 * `cls` is a space-separated class list — `msg msg-a`, `msg msg-a crew-reply` —
 * and other consumers already match a single class with a whitespace-bounded
 * test. Today's producer emits this token on its own, so equality would match
 * too; membership is defensive against the token later arriving alongside
 * others, which would silently kill the guard while tests stayed green.
 *
 * The value lives here rather than inline at the call site so the tree has
 * exactly one spelling of it.
 */
export const RECONCILE_NOTE_CLS = 'reconcile-note'

/**
 * True when `cls` carries the note class as a whole class.
 *
 * Splitting is what makes this a class test rather than a substring test:
 * `reconcile-note-draft` contains the token but is a different class.
 */
export function isReconcileNote(cls: string | undefined | null): boolean {
  if (typeof cls !== 'string' || cls.length === 0) return false
  return cls.trim().split(/\s+/).includes(RECONCILE_NOTE_CLS)
}

/**
 * True when a row IS a note, live or rehydrated from disk.
 *
 * `cls` alone is not sufficient after a restart: history persists `cls` only for
 * `role === "system"`, so a note (appended as `inject`) reaches disk with its
 * class dropped and its `meta` kept. A `cls`-only gate is therefore true on the
 * websocket frame and false on the very same row after a reload — options
 * vanish and the raw marker renders.
 *
 * `meta.noteSession` is the surviving half of the contract, and it discriminates:
 * the note append and its deferred-flush twin are its only producers, so the
 * other `inject` producers (refusal recovery, cron, replay, synthesis, hook
 * halt) cannot be re-admitted by it — the synthesis row carries `injectKind`,
 * a different key.
 */
export function isNoteRow(m: { cls?: string | null; meta?: Record<string, unknown> | null }): boolean {
  if (isReconcileNote(m.cls)) return true
  const session = m.meta?.noteSession
  return typeof session === 'string' && session.length > 0
}
