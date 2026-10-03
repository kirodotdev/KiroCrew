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

// The hosts the server's CSP `frame-src` permits an embedded pane on, matched
// EXACTLY against `_LOOPBACK_FRAME_SRC` / `_INSTANCES_FRAME_SRC_EXTRA` in
// src/kiro_crew/dashboard/server.py — not a superset:
//   - 127.0.0.1, localhost, 0.0.0.0 are admitted under BOTH http and https;
//   - a single-label `*.localhost` name is admitted under http ONLY;
//   - IPv6 loopback [::1] is deliberately NOT admitted (a bracketed IPv6 host
//     with a wildcard port is invalid CSP grammar, so the server omits it).
// A host/scheme pair outside this set has the browser refuse the pane frame
// before any gateway check runs, so the pane cannot embed there.
const IPV4_LOOPBACK_HOST_RE = /^(?:127\.0\.0\.1|localhost|0\.0\.0\.0)$/
const DOT_LOCALHOST_HOST_RE = /^[a-z0-9-]+\.localhost$/

/**
 * Return true if a pane iframe can embed under the parent dashboard's own
 * (protocol, host) — i.e. the pair is in the server's CSP `frame-src` loopback
 * set. `protocol` is a `window.location.protocol` value ("http:" / "https:");
 * `host` is a `window.location.hostname` (no brackets, no port). The caller
 * checks this before mounting the iframe, to decide whether the pane can embed
 * at all rather than mounting a frame the browser will refuse.
 */
export function isEmbeddableLoopbackOrigin(protocol: string, host: string): boolean {
  if (typeof protocol !== 'string' || typeof host !== 'string') return false
  if (protocol !== 'http:' && protocol !== 'https:') return false
  // IPv4 loopback: http or https.
  if (IPV4_LOOPBACK_HOST_RE.test(host)) return true
  // *.localhost: http only (the CSP extra is http-only).
  if (protocol === 'http:' && DOT_LOCALHOST_HOST_RE.test(host)) return true
  return false
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
