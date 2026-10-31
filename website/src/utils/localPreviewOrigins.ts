/**
 * The Preview tab's allowlist, `dashboard.browser_local_origins`: the one rule
 * for what an entry looks like and whether a URL falls under the list. Shared by
 * Settings -> Browser (which edits the list) and the side panel's Preview tab
 * (which frames only what the list allows), so the two cannot disagree.
 */

/**
 * The shape one Preview-tab origin must have, as the gateway's
 * `BROWSER_LOCAL_ORIGIN_PATTERN` spells it: a loopback host and a port. Kept in
 * step with the backend rule by a backend test on the pattern and this module's
 * own test on the same examples.
 */
const ORIGIN_RE = /^(localhost|127\.0\.0\.1|\[::1\]):([0-9]{1,5})$/

/**
 * Canonicalize a typed origin, or null when it is not one the gateway keeps.
 *
 * Trims, lower-cases, and accepts a pasted `http://localhost:5173/` by stripping
 * the scheme and path, because that is what a dev server prints and the field is
 * for pasting it. A port outside 1-65535, any other host, a path with content, or
 * a query are refused rather than repaired.
 */
export function normalizeLocalOrigin(raw: string): string | null {
  let text = raw.trim().toLowerCase()
  if (!text) return null
  if (text.startsWith('http://') || text.startsWith('https://')) {
    try {
      const u = new URL(text)
      if ((u.pathname && u.pathname !== '/') || u.search || u.hash || u.username || u.password) return null
      text = u.host
    } catch {
      return null
    }
  }
  const m = ORIGIN_RE.exec(text)
  if (!m) return null
  const port = Number(m[2])
  if (!(port > 0 && port < 65536)) return null
  return `${m[1]}:${port}`
}

/** Whether a URL is one the Preview tab may frame under the configured origins. */
export function previewAllowsUrl(url: string, origins: readonly string[]): boolean {
  let u: URL
  try {
    u = new URL(url)
  } catch {
    return false
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
  const host = u.hostname.toLowerCase()
  // The URL API already brackets an IPv6 hostname (`[::1]`); only a bare form
  // (an older engine) needs the brackets added, never a second pair.
  const hostText = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host
  const port = u.port || (u.protocol === 'https:' ? '443' : '80')
  return origins.includes(`${hostText}:${port}`)
}
