/**
 * ssrf-guard.mjs — address-validate EVERY browser request, not just the entry URL.
 *
 * The backend validates the URL/repo the operator types, but Chromium then follows
 * redirects and loads subresources on its own: a public page can 3xx to
 * http://10.0.0.1/ or embed a private-network subresource (169.254.169.254 cloud
 * metadata, a LAN box) and the browser would fetch it server-side. This installs a
 * request interceptor on a Playwright context/page that resolves each request's
 * host and aborts anything on an internal/private address. Loopback stays allowed:
 * the localhost preview and the repo's own loopback static server are the feature.
 *
 * Mirrors the backend `_url_target_allowed` rule (allow loopback + public; reject
 * private / link-local incl. 169.254.169.254 / reserved / multicast / unspecified).
 */
import dns from 'node:dns/promises'
import net from 'node:net'

function ipv4Disallowed(ip) {
  const p = ip.split('.').map(Number)
  if (p.length !== 4 || p.some(n => Number.isNaN(n) || n < 0 || n > 255)) return true
  const [a, b, c] = p
  if (a === 127) return false            // loopback — allowed (localhost preview)
  if (a === 10) return true              // private 10/8
  if (a === 172 && b >= 16 && b <= 31) return true   // private 172.16/12
  if (a === 192 && b === 168) return true            // private 192.168/16
  if (a === 169 && b === 254) return true            // link-local incl. metadata
  if (a === 100 && b >= 64 && b <= 127) return true  // CGNAT 100.64/10
  if (a === 0) return true               // 0.0.0.0/8 this-network
  if (a === 192 && b === 0 && c === 0) return true   // IETF protocol 192.0.0/24
  if (a === 192 && b === 0 && c === 2) return true   // TEST-NET-1 192.0.2/24
  if (a === 192 && b === 88 && c === 99) return true // 6to4 relay anycast 192.88.99/24
  if (a === 198 && (b === 18 || b === 19)) return true  // benchmark 198.18/15
  if (a === 198 && b === 51 && c === 100) return true   // TEST-NET-2 198.51.100/24
  if (a === 203 && b === 0 && c === 113) return true    // TEST-NET-3 203.0.113/24
  if (a >= 224) return true              // multicast 224/4 + reserved 240/4 + 255.255.255.255
  return false                           // public
}

function ipv6Disallowed(ip) {
  const s = ip.toLowerCase().replace(/^\[|\]$/g, '')
  if (s === '::1') return false          // loopback — allowed
  if (s === '::') return true            // unspecified
  if (/^fe[89ab]/.test(s)) return true   // link-local fe80::/10
  if (/^fe[c-f]/.test(s)) return true    // site-local fec0::/10 (deprecated)
  if (/^f[cd]/.test(s)) return true      // unique-local fc00::/7
  if (/^ff/.test(s)) return true         // multicast
  const dotted = s.match(/::ffff:(\d+\.\d+\.\d+\.\d+)$/)  // IPv4-mapped, dotted
  if (dotted) return ipv4Disallowed(dotted[1])
  const hex = s.match(/::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/)  // IPv4-mapped, hex
  if (hex) {
    const hi = parseInt(hex[1], 16), lo = parseInt(hex[2], 16)
    return ipv4Disallowed([(hi >> 8) & 255, hi & 255, (lo >> 8) & 255, lo & 255].join('.'))
  }
  if (/^2002:/.test(s)) {                // 6to4 — decode the embedded IPv4 and vet it
    const m = s.match(/^2002:([0-9a-f]{1,4}):([0-9a-f]{1,4})/)
    if (!m) return true                  // undecodable 6to4 → fail closed
    const hi = parseInt(m[1], 16), lo = parseInt(m[2], 16)
    return ipv4Disallowed([(hi >> 8) & 255, hi & 255, (lo >> 8) & 255, lo & 255].join('.'))
  }
  return false                           // global v6 — allowed
}

function ipDisallowed(ip) {
  const kind = net.isIP(ip)
  if (kind === 4) return ipv4Disallowed(ip)
  if (kind === 6) return ipv6Disallowed(ip)
  return true                            // not an IP literal — refuse
}

function isLoopbackIp(ip) {
  const kind = net.isIP(ip)
  if (kind === 4) return ip.split('.')[0] === '127'
  if (kind === 6) {
    const s = ip.toLowerCase().replace(/^\[|\]$/g, '')
    if (s === '::1') return true
    const m = s.match(/::ffff:(\d+\.\d+\.\d+\.\d+)$/)
    return !!m && m[1].split('.')[0] === '127'
  }
  return false
}

// Three-way verdict: 'public' | 'loopback' | 'internal'. Resolves ALL addresses;
// a name that resolves to any disallowed non-loopback address is 'internal', and
// a name mixing loopback with a public address is also 'internal' (rebinding-safe).
async function hostVerdict(hostname) {
  let addrs
  try {
    addrs = await dns.lookup(hostname, { all: true })
  } catch {
    return 'internal'
  }
  if (!addrs.length) return 'internal'
  let anyLoop = false
  let anyPublic = false
  for (const a of addrs) {
    if (isLoopbackIp(a.address)) { anyLoop = true; continue }
    if (ipDisallowed(a.address)) return 'internal'
    anyPublic = true
  }
  if (anyLoop) return anyPublic ? 'internal' : 'loopback'
  return 'public'
}

// `target` is a Playwright BrowserContext or Page (both expose `.route`).
// `allowOrigin` (e.g. "http://127.0.0.1:5173") is the ONLY origin for which a
// loopback request is permitted — the local build server, or a localhost-preview
// base — so a public page cannot reach some OTHER service on 127.0.0.1. Every
// request/redirect/subresource is re-resolved here; a public host is allowed,
// an internal one is aborted.
export async function installSsrfGuard(target, allowOrigin = null) {
  await target.route('**/*', async (route) => {
    try {
      const u = new URL(route.request().url())
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return route.abort('blockedbyclient')
      const verdict = await hostVerdict(u.hostname)
      if (verdict === 'public') return route.continue()
      if (verdict === 'loopback') {
        return (allowOrigin && u.origin === allowOrigin) ? route.continue() : route.abort('blockedbyclient')
      }
      return route.abort('blockedbyclient')
    } catch {
      return route.abort('blockedbyclient')
    }
  })
  // HTTP route() does not intercept WebSockets, so a rendered page could open a
  // ws:// to an internal host and read it back. Apply the same host verdict to
  // every WebSocket: connect to the server only when allowed, else close.
  if (typeof target.routeWebSocket === 'function') {
    await target.routeWebSocket(/.*/, (ws) => {
      void (async () => {
        try {
          const u = new URL(ws.url())
          const verdict = await hostVerdict(u.hostname)
          // Loopback is scoped to the allowed base's host:port (schemes differ
          // for ws vs http, so origin can't match) — the preview server's own
          // HMR socket is permitted; any other loopback port/host is not.
          let sameBase = false
          if (allowOrigin) {
            try {
              const b = new URL(allowOrigin)
              sameBase = b.hostname === u.hostname && (b.port || '') === (u.port || '')
            } catch { sameBase = false }
          }
          const ok = verdict === 'public' || (verdict === 'loopback' && sameBase)
          if (ok) ws.connectToServer()
          else ws.close({ code: 1008, reason: 'blockedbyclient' })
        } catch {
          ws.close({ code: 1008, reason: 'blockedbyclient' })
        }
      })()
    })
  }
}

