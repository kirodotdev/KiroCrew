/** Compose intent: the user focused or typed in a chat's composer.
 *
 *  A chat whose session the idle sweep reclaimed while its window kept focus
 *  gets no new focus, tab or window event, so its first send would wait out
 *  the whole `session/load`. The websocket hook binds a sender here
 *  (`attachFocusRelay` in hooks/websocket/attention.ts) that re-announces the
 *  slot so the server re-arms its resume prefetch; that sender owns the
 *  throttle and the gating. Before it binds (or after it unbinds) this is a
 *  no-op: compose intent is a best-effort optimization, never load-bearing. */
let sender: (slot: string) => void = () => {}

export function bindComposeIntentSender(fn: ((slot: string) => void) | null): void {
  sender = fn ?? (() => {})
}

export function emitComposeIntent(slot: string | null | undefined): void {
  if (slot) sender(slot)
}
