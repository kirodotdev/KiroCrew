/**
 * connectInstanceInto — THE single "bring one tunnel up and record it" step,
 * shared by every path that connects an instance: the manual select/reconnect
 * (useSelectInstance, driven by tab clicks and the ⌘/Ctrl+digit chord) and the
 * proactive auto-connect (useAutoConnectInstances). Keeping both on one unit is
 * the same "single owner" discipline useSelectInstance already documents — a
 * future change to how a connect result maps onto the warm store lands in both
 * automatically instead of drifting.
 *
 * Contract:
 *  - Fires POST /api/instances/{id}/connect (idempotent server-side: tunnel-up
 *    → validate-over-tunnel → re-mint token → 502 on genuine failure).
 *  - On a `connected` result carrying a live port + token, writes the `warm`
 *    entry so the pane can render its iframe with no further round-trip.
 *  - NEVER touches `activeId`. Selection is a separate concern owned by the
 *    caller (useSelectInstance activates the pane; auto-connect must not yank
 *    the user to a background tunnel it just raised).
 *  - Returns the tunnel status so callers can branch (e.g. surface the error).
 *    Rejections propagate — react-query's mutation and the auto-connect fan-out
 *    each handle failure their own way (in-pane error panel / silent backoff).
 */
import { api } from '../api/client'
import { setWarm, type WarmConn } from '../store/instancesSlice'
import type { AppDispatch } from '../store'

export async function connectInstanceInto(dispatch: AppDispatch, id: string) {
  const st = await api.connectInstance(id)
  if (st.state === 'connected' && st.local_port && st.token) {
    const conn: WarmConn = { port: st.local_port, token: st.token }
    dispatch(setWarm({ id, conn }))
  }
  return st
}
