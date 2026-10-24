/** Pure readers of a slot row's identity fields, shared by the dashboard
 *  slice, the chat selectors and the pages. `dashboardSlice` re-exports both,
 *  so that stays the import path callers use. */

/**
 * Resolve a slot's surface key. Backend emits `surface` (mirrors `mode` today
 * but lets the two diverge later); fall back to `mode` for slots delivered
 * before the backend rollout. Empty string is the canonical "main chat" key.
 */
export function slotSurfaceKey(slot: { mode?: string; surface?: string }): string {
  return slot.surface ?? slot.mode ?? ''
}

/**
 * True for a relay archive: a chat an older build ran on a connected crew,
 * read-only now. Two client surfaces must agree with each other and with the
 * server on it: `selectContinuable` (which must not OFFER Continue on one) and
 * ChatPage's regenerate / edit-resend gates (which must not offer those
 * either). Mirrors `relay_archive_refusal` in
 * `src/kiro_crew/dashboard/relay_archive.py`, which REFUSES the same actions
 * with 409 `relay_archive_read_only`.
 *
 * Keyed on `executor` (the binding INTENT), never `instance_id`:
 * the server refuses a half-open binding (marker set, triple incomplete) too, so
 * an unbound `executor` must read as bound here exactly as it does there. An
 * absent slot is not bound — an older gateway ships `executor` on every slot, so
 * only a genuinely missing lookup lands here, and a missing slot has no local
 * action to gate.
 */
export function slotIsRemoteBound(slot: { executor?: string } | null | undefined): boolean {
  return slot?.executor === 'remote'
}
