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
import { paneAccessFor, parsePaneEndpoint, resolvePaneMode } from './paneChannel'
import type { AppDispatch } from '../store'
import type { InstanceTunnelStatus } from '../api/client/instances'

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

  // How this parent addresses panes decides the warm endpoint's shape. In
  // same-origin-relay mode the owner-only issuer (openInstancePane) connects the
  // tunnel AND mints a capability in one call and returns a discriminated relay
  // endpoint (no port, no token). In direct-loopback mode the connect route is
  // used exactly as before, so that path is byte-identical.
  const mode = resolvePaneMode(window.location)
  if (mode.kind === 'same-origin-relay') {
    // Auto-warm and renewal must never BRING a tunnel up, and must never receive
    // a remote token. Both are served by ONE atomic gateway call: the
    // connected-only issue mode (`onlyIfConnected`) mints a relay pane only for
    // an already-connected forward — decided under the manager lock, so a
    // background issue racing an explicit disconnect can neither reconnect the
    // tunnel nor surface a token — and declines (a non-connected 200, no
    // endpoint) otherwise. The earlier design gated `openInstancePane` behind a
    // SEPARATE connect probe, which both received the connect route's remote
    // token and left a disconnect-shaped race between the probe and the (always
    // connecting) issue. A plain selection/Retry issue keeps connect-or-create.
    let raw
    try {
      raw = await api.openInstancePane(
        id,
        paneAccessFor(mode),
        rebuild ? { rebuild: true } : onlyIfConnected ? { onlyIfConnected: true } : undefined,
      )
    } catch (err) {
      // A non-2xx (remote_upgrade_required, bad_request, connect failure) throws
      // here; the caller's mutation / fan-out surfaces its `.code` to the user.
      paneLog('warm-failed', { id, via, rebuild: rebuild || undefined, error: (err as Error)?.message || 'unknown' })
      throw err
    }
    const endpoint = parsePaneEndpoint(raw)
    if (endpoint && endpoint.kind === 'same-origin-relay') {
      dispatch(setWarm({ id, conn: endpoint }))
      paneLog('warm', { id, via, rebuild: rebuild || undefined, relay: true })
      // Callers branch only on `state`; a valid relay endpoint means connected.
      const connected: InstanceTunnelStatus = { instance_id: id, state: 'connected' }
      return connected
    }
    // No relay endpoint. Under `onlyIfConnected` this is the gateway declining a
    // forward that is not up (its body carries the real non-connected state);
    // otherwise it is a 200 whose shape we could not validate. Either way, leave
    // any previous warm entry standing. Report the declined state so a background
    // caller branches on it (auto-warm/renewal treat non-connected as "nothing to
    // warm") rather than believing the pane came up.
    const declinedState =
      (raw && typeof raw === 'object' && (raw as { state?: string }).state) || undefined
    paneLog('warm-declined', {
      id,
      via,
      state: declinedState,
      reason: onlyIfConnected ? 'not_connected' : 'no_relay_endpoint',
    })
    const synthetic: InstanceTunnelStatus = {
      instance_id: id,
      // A plain issue that reached a 200 kept the old "connected" contract; a
      // connected-only decline reports the forward's real (non-connected) state.
      state: onlyIfConnected ? ((declinedState as InstanceTunnelStatus['state']) || 'disconnected') : 'connected',
    }
    return synthetic
  }

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
    const conn: WarmConn = { kind: 'direct-loopback', port: st.local_port, token: st.token }
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
