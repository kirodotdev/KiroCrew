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
 *  - Journals the outcome through `paneLog` under `via`. Every warm-writer —
 *    the manual select, the auto-connect fan-out, the viewport's auto-warm and
 *    its Retry — funnels through here, so the journal block exists once. Before
 *    this unit journaled, it was the one silent warm path: a
 *    connect that came back `connected` but with no port or no token left the
 *    PREVIOUS warm entry (a dead port) standing, so the pane kept its stale src,
 *    the tab still rendered an iframe, and the user saw only "loading" — with
 *    nothing in the journal, because the viewport's own warm paths log and this
 *    one did not. A rejection is journaled here too, then re-thrown unchanged.
 */
import { api } from '../api/client'
import { paneLog } from './paneLog'
import { setWarm, type WarmConn } from '../store/instancesSlice'
import type { AppDispatch } from '../store'

/**
 * Which caller asked. One checked vocabulary for the journal's `via` field, so
 * a log reader learns four names once: `select` (tab click / ⌘-digit chord),
 * `auto-connect` (the web-app-load fan-out), `auto-warm` (the viewport
 * pre-mounting already-connected panes after a poll), `retry` (the in-pane
 * error panel's Retry button).
 */
export type ConnectVia = 'select' | 'auto-connect' | 'auto-warm' | 'retry'

/**
 * `rebuild` asks the gateway to tear the existing tunnel down and spawn a fresh
 * forwarder on a different local port (the freed one is excluded from the
 * allocation) before answering, instead of the idempotent
 * "already connected, here is its status". Only Retry sets it, and only after a
 * load watchdog fired on a document that DID navigate — the case where every
 * probe says the tunnel is healthy yet the pane never finishes loading its
 * module graph (one stalled stream). The journal carries the flag so a later
 * `warm` line can be read as "new tunnel" rather than "same tunnel again".
 *
 * `onlyIfConnected` is the mirror image, for auto-warm: answer an already-up
 * tunnel exactly like a plain connect, but never bring one up and never touch
 * the connect intent. The gateway decides under its own manager lock, so an
 * auto-warm racing an explicit disconnect cannot re-open the tunnel the user
 * just closed; a tunnel that is not up comes back as a non-connected status,
 * which this step journals as `warm-declined` and leaves the pane alone.
 */
export async function connectInstanceInto(
  dispatch: AppDispatch,
  id: string,
  via: ConnectVia = 'select',
  opts: { rebuild?: boolean; onlyIfConnected?: boolean } = {},
) {
  const rebuild = !!opts.rebuild
  const onlyIfConnected = !!opts.onlyIfConnected
  let st
  try {
    // The options object is passed only when set, so the plain call keeps the
    // signature every existing caller and test spies on.
    st = await (rebuild
      ? api.connectInstance(id, { rebuild: true })
      : onlyIfConnected
        ? api.connectInstance(id, { onlyIfConnected: true })
        : api.connectInstance(id))
  } catch (err) {
    paneLog('warm-failed', { id, via, rebuild: rebuild || undefined, error: (err as Error)?.message || 'unknown' })
    throw err
  }
  if (st.state === 'connected' && st.local_port && st.token) {
    const conn: WarmConn = { port: st.local_port, token: st.token }
    dispatch(setWarm({ id, conn }))
    paneLog('warm', { id, port: st.local_port, via, rebuild: rebuild || undefined })
  } else {
    // Same shape as the viewport's own `warm-declined`: the response says
    // something other than "connected with a port and a token", and whatever
    // warm entry existed before is left exactly as it was.
    paneLog('warm-declined', {
      id,
      via,
      state: st.state,
      hasPort: !!st.local_port,
      hasToken: !!st.token,
      error: st.error || undefined,
      reason: st.diagnosis?.reason || undefined,
    })
  }
  return st
}
