/**
 * useDashboardHealthProbe — same-tab self-recovery from gateway-disconnect.
 *
 * Problem this solves:
 *   When the WebSocket drops (gateway restart, network blip, dev reload), the
 *   dashboard renders the "Gateway offline — reconnecting" banner and
 *   `useWebSocket` retries with exponential backoff (1s →
 *   2s → 4s → 8s → 10s capped, infinite).
 *
 *   The browser WebSocket API can't see the HTTP status of a failed upgrade
 *   handshake (just `onclose`), so a 401/403 looks identical to a connection
 *   refused. If the gateway restarted, its in-memory HMAC secret regenerates
 *   and existing session cookies become invalid — every WS reconnect attempt
 *   then fails with 401, the loop just keeps retrying forever, and the user
 *   has no in-tab signal that they need to re-auth.
 *
 *   A prior change addressed this for the Electron desktop app via
 *   `did-navigate`. Web browser users (e.g. via SSH tunnel) get nothing.
 *
 * Recovery mechanism:
 *   While `dashboard.connected === false`, poll a lightweight HTTP endpoint
 *   (`/api/status`) every `INTERVAL_MS`. The response carries the standard
 *   auth headers, so:
 *     - 200: gateway is back up AND auth is valid → call `forceReconnect()`
 *       on the WS hook to reset the backoff timer and reconnect immediately.
 *     - 403 with `X-Auth-Required: true`: gateway is back up but the cookie
 *       is invalid. The existing `checkSessionExpired` helper in
 *       `api/client.ts` already injects the in-tab auth banner, so the user
 *       can paste a fresh `kirocrew token` URL without leaving the tab. Once
 *       they do, the page navigates to `?token=X`, gets a fresh cookie, and
 *       reloads — recovery complete.
 *     - Network error / non-403 4xx-5xx: gateway still down or in a bad
 *       state. Stay quiet, keep polling.
 *
 *   When `dashboard.connected === true` (WS is healthy), the polling loop
 *   stops — no wasted requests during normal operation.
 *
 * Why poll instead of relying on WS reconnect alone:
 *   1. WS reconnect is blind to auth — it can't surface "you need to re-auth"
 *      to the user. The HTTP probe trips the existing 403 handler that
 *      shows the token-paste banner.
 *   2. WS reconnect backoff caps at 10s. The probe runs at 3s so recovery
 *      after gateway restart is ~3s instead of 10s.
 *   3. The probe is cheap (single GET to a sub-millisecond handler) and only
 *      runs while disconnected, so steady-state cost is zero.
 */
import { useEffect, useRef } from 'react'
import { useConnected } from './useConnected'
import { api } from '../api/client'

const INTERVAL_MS = 3000

export function useDashboardHealthProbe(forceReconnect: () => void): void {
  const connected = useConnected()
  // Track whether the WS has ever been connected this session. dashboardSlice
  // initial state is `connected: false`, which is also the same value during
  // a fresh page load before the very first WS handshake completes. Without
  // this gate, the probe would fire on every page load -- hit /api/status,
  // get a 200, and call forceReconnect() while useWebSocket's mount effect
  // still has its initial WS in CONNECTING state. forceReconnect tears that
  // in-flight WS down and starts a new one, adding latency and churn to
  // every normal page load. By only enabling the probe AFTER we've seen
  // connected=true at least once, we restrict it to genuine post-connect
  // disconnects (gateway restart, network blip, dev reload) -- exactly the
  // self-recovery cases this hook exists to handle.
  const hasEverConnectedRef = useRef(false)
  if (connected) {
    hasEverConnectedRef.current = true
  }

  useEffect(() => {
    if (connected) return  // healthy -- no polling needed
    if (!hasEverConnectedRef.current) return  // initial handshake; not a recovery scenario

    let cancelled = false
    const probe = async () => {
      if (cancelled) return
      try {
        await api.status()
        // Success path: gateway is up, cookie is valid. Wake the WS up
        // immediately rather than waiting for its backoff tick. The 200
        // response also auto-dismisses any stale auth banner via the j
        // wrapper in api/client.ts.
        //
        // Set `cancelled = true` BEFORE forceReconnect so the interval fires
        // at most once per disconnect cycle. Without this, if the WS handshake
        // takes longer than INTERVAL_MS (e.g. over an SSH tunnel), the next
        // tick would call api.status() → succeed → call forceReconnect()
        // again, tearing down the in-progress WS and starting a new one in a
        // loop. The cleanup runs when `connected` flips back to true; if it
        // later flips to false again the effect re-runs and the probe restarts
        // cleanly with cancelled=false.
        if (!cancelled) {
          cancelled = true
          forceReconnect()
        }
      } catch {
        // Failure path:
        //   - 403 with X-Auth-Required: api/client.ts checkSessionExpired
        //     has already shown the in-tab token banner. Nothing for us
        //     to do — the user pastes a token, page reloads, recovery.
        //   - Network error / other: gateway still down, keep polling.
      }
    }

    // Probe immediately on disconnect (don't wait INTERVAL_MS for the first
    // attempt — common case is a brief blip and we want to recover fast).
    probe()
    const id = setInterval(probe, INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [connected, forceReconnect])
}
