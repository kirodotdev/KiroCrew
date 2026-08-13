/**
 * Loopback-origin validation for the Instances `postMessage` unread relay.
 *
 * The embedded remote dashboards relay their unread count to the parent via
 * `window.parent.postMessage`. The parent MUST validate `event.origin` before
 * trusting any message (§5.4 "postMessage hardening"): only accept origins that
 * are exactly `http://127.0.0.1:<port>` for a port we currently have a warm
 * tunnel on. This pure helper isolates the parsing so it is unit-testable and
 * can't drift into accepting `https`, a hostname, a path, or a port we don't own.
 */

// Exact match: http, a loopback host (127.0.0.1, localhost, or a single-label
// *.localhost name -- all resolve to the loopback the SSH forward binds), a numeric
// port, nothing else. The embedded pane uses the parent dashboard's own hostname (see
// InstancesViewport `srcFor`) so it is same-site with the parent; this allowlist must
// therefore accept the same loopback names users open the dashboard on, not only
// 127.0.0.1. Port ownership (a currently-warm tunnel) remains the primary gate.
const LOOPBACK_ORIGIN_RE = /^http:\/\/(?:127\.0\.0\.1|localhost|[a-z0-9-]+\.localhost):(\d{1,5})$/

/**
 * Return the port number if *origin* is exactly a loopback http origin, else
 * null. Rejects https, hostnames (localhost), trailing paths, and out-of-range
 * ports.
 */
export function parseLoopbackOriginPort(origin: string): number | null {
  if (typeof origin !== 'string') return null
  const m = LOOPBACK_ORIGIN_RE.exec(origin)
  if (!m) return null
  const port = Number(m[1])
  if (!Number.isInteger(port) || port < 1 || port > 65535) return null
  return port
}

/**
 * Resolve a validated message origin to one of our warm instance ids.
 *
 * @param origin   the untrusted `event.origin`
 * @param portToId map of loopback port → instance id for *currently warm* tunnels
 * @returns the instance id if the origin is a known tunnel origin, else null
 */
export function resolveTunnelOrigin(
  origin: string,
  portToId: Map<number, string>,
): string | null {
  const port = parseLoopbackOriginPort(origin)
  if (port === null) return null
  return portToId.get(port) ?? null
}
